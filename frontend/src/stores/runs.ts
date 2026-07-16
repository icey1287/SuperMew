import { defineStore } from 'pinia';
import {
  applyRunEvent,
  initialRunEventState,
  type RunEventState,
  type RunLifecycleStatus,
  type RunTransportStatus,
  type RuntimeRunEvent,
} from '@/events/runEventReducer';
import { connectRunEventStream } from '@/events/runEventStream';
import {
  cancelRun,
  createIdempotencyKey,
  createRun,
  getRun,
  getRunEvents,
  resumeRun,
} from '@/runs/runClient';
import type {
  CreateRunCommand,
  ResumeRunCommand,
  RunCreateResponse,
  RunRecord,
  RunResumeResponse,
} from '@/types/runs';
import { normalizePublicErrorInfo } from '@/types/publicError';
import { getPublicError } from '@/utils/api';

type UnknownRecord = Record<string, unknown>;

interface PendingCreateAttempt {
  message: string;
  idempotencyKey: string;
}

interface PendingResumeAttempt {
  hitlToken: string;
  answer: string;
  idempotencyKey: string;
}

const streamControllers = new WeakMap<object, Map<string, AbortController>>();

function controllersFor(store: object): Map<string, AbortController> {
  let controllers = streamControllers.get(store);
  if (!controllers) {
    controllers = new Map();
    streamControllers.set(store, controllers);
  }
  return controllers;
}

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function lifecycleStatus(value: unknown): RunLifecycleStatus | null {
  const status = String(value || '');
  if (status === 'succeeded') return 'completed';
  if (
    [
      'idle',
      'creating',
      'queued',
      'pending',
      'running',
      'waiting_input',
      'cancelling',
      'cancelled',
      'failed',
      'completed',
    ].includes(status)
  ) {
    return status as RunLifecycleStatus;
  }
  return null;
}

function authToken(explicit?: string): string {
  if (explicit !== undefined) return explicit;
  return typeof localStorage === 'undefined' ? '' : localStorage.getItem('accessToken') || '';
}

export const useRunsStore = defineStore('runs', {
  state: () => ({
    byId: {} as Record<string, RunEventState>,
    pendingCreates: {} as Record<string, PendingCreateAttempt>,
    pendingResumes: {} as Record<string, PendingResumeAttempt>,
  }),
  getters: {
    activeForThread: (state) => (threadId: string | null) => {
      if (!threadId) return null;
      return (
        Object.values(state.byId).find(
          (run) =>
            run.threadId === threadId &&
            !run.terminal &&
            !['idle', 'cancelled', 'failed', 'completed'].includes(run.status)
        ) || null
      );
    },
    isStreamingForThread: (state) => (threadId: string | null) => {
      if (!threadId) return false;
      return Object.values(state.byId).some(
        (run) =>
          run.threadId === threadId &&
          !run.terminal &&
          ['connecting', 'open', 'reconnecting'].includes(run.transportStatus)
      );
    },
    failureForRun: (state) => (runId: string) => state.byId[runId]?.error || null,
    canRetry: (state) => (runId: string) => {
      const run = state.byId[runId];
      return Boolean(run?.status === 'failed' && run.error?.retryable);
    },
  },
  actions: {
    ensure(runId: string, threadId: string): RunEventState {
      if (!this.byId[runId]) {
        this.byId[runId] = initialRunEventState(runId, threadId);
      }
      return this.byId[runId];
    },

    apply(event: RuntimeRunEvent): RunEventState {
      const current = this.ensure(event.run_id, event.thread_id);
      const next = applyRunEvent(current, event);
      this.byId[event.run_id] = next;
      if (event.type === 'hitl.resumed' && next.lastSequence === event.sequence) {
        delete this.pendingResumes[event.run_id];
      }
      return next;
    },

    setTransport(runId: string, status: RunTransportStatus, attempt = 0, error: unknown = null) {
      const current = this.byId[runId];
      if (!current) return;
      current.transportStatus = status;
      current.reconnectAttempt = Math.max(attempt, 0);
      current.transportError = error
        ? normalizePublicErrorInfo(error, {
            code: 'NETWORK_UNAVAILABLE',
            retryable: true,
            category: 'stream',
          })
        : null;
    },

    hydrate(runId: string, payload: unknown): RunEventState | null {
      const data = asRecord(payload);
      if (!data) return this.byId[runId] || null;
      const threadId = typeof data.thread_id === 'string' ? data.thread_id : '';
      const current = this.byId[runId] || (threadId ? this.ensure(runId, threadId) : null);
      if (!current) return null;

      const status = lifecycleStatus(data.status) || current.status;
      const incomingTerminal = ['completed', 'failed', 'cancelled'].includes(status);
      if (current.terminalSequence !== null && (!incomingTerminal || status !== current.status)) {
        return current;
      }
      current.status = status;
      current.terminal = incomingTerminal;
      if (typeof data.idempotency_key === 'string') {
        current.idempotencyKey = data.idempotency_key;
      }

      if (status === 'completed') {
        current.error = null;
      } else if (data.error || data.error_code || status === 'failed' || status === 'cancelled') {
        current.error = normalizePublicErrorInfo(data, {
          code: status === 'cancelled' ? 'RUN_CANCELLED' : 'RUN_EXECUTION_FAILED',
          retryable: status === 'cancelled' ? false : undefined,
          category: 'run',
        });
      } else if (!current.terminal) {
        current.error = null;
      }
      return current;
    },

    async create(command: CreateRunCommand): Promise<{
      response: RunCreateResponse;
      idempotencyKey: string;
      state: RunEventState;
    }> {
      const existing = this.pendingCreates[command.threadId];
      if (existing && existing.message !== command.message) {
        throw getPublicError({
          code: 'CONFLICT',
          retryable: false,
          category: 'run',
        });
      }
      if (
        existing &&
        command.idempotencyKey &&
        command.idempotencyKey !== existing.idempotencyKey
      ) {
        throw getPublicError({
          code: 'CONFLICT',
          retryable: false,
          category: 'run',
        });
      }
      const idempotencyKey =
        existing?.idempotencyKey || command.idempotencyKey || createIdempotencyKey('run');
      this.pendingCreates[command.threadId] = {
        message: command.message,
        idempotencyKey,
      };

      try {
        const response = await createRun(
          command.threadId,
          {
            message: command.message,
            idempotency_key: idempotencyKey,
            expected_thread_version: command.expectedThreadVersion,
            multitask_strategy: command.multitaskStrategy || 'reject',
            on_disconnect: command.onDisconnect || 'continue',
            approved_tools: command.approvedTools || [],
          },
          command.token
        );
        delete this.pendingCreates[command.threadId];
        const state = this.ensure(response.run.id, response.run.thread_id);
        state.idempotencyKey = idempotencyKey;
        this.hydrate(response.run.id, response.run);
        return { response, idempotencyKey, state };
      } catch (error) {
        throw getPublicError(error);
      }
    },

    async start(command: CreateRunCommand): Promise<RunCreateResponse> {
      const created = await this.create(command);
      await this.connect(created.response.run.id, command.token);
      return created.response;
    },

    async get(runId: string, token?: string): Promise<RunRecord> {
      const response = await getRun(runId, authToken(token));
      this.hydrate(runId, response);
      return response;
    },

    async replay(runId: string, token?: string): Promise<RunEventState> {
      let current = this.byId[runId];
      if (!current) {
        const run = await this.get(runId, token);
        current = this.ensure(run.id, run.thread_id);
      }
      if (current.lastSequence === 0 && current.terminal) {
        const idempotencyKey = current.idempotencyKey;
        const transportStatus = current.transportStatus;
        this.byId[runId] = initialRunEventState(runId, current.threadId);
        this.byId[runId].idempotencyKey = idempotencyKey;
        this.byId[runId].transportStatus = transportStatus;
        current = this.byId[runId];
      }

      while (true) {
        const response = await getRunEvents(runId, authToken(token), {
          after: current.lastSequence,
          limit: 1000,
        });
        if (!response.events.length) return current;
        for (const event of response.events) {
          const before = current.lastSequence;
          current = this.apply(event);
          if (event.sequence > before && current.lastSequence === before) {
            throw getPublicError({
              code: 'STREAM_PROTOCOL_ERROR',
              retryable: true,
              category: 'stream',
            });
          }
        }
        if (response.events.length < 1000) return current;
      }
    },

    async connect(runId: string, token?: string): Promise<RunEventState> {
      const current = this.byId[runId];
      if (!current) {
        throw getPublicError({
          code: 'NOT_FOUND',
          retryable: false,
          category: 'run',
        });
      }
      if (current.terminalSequence !== null) return current;

      const controllers = controllersFor(this);
      controllers.get(runId)?.abort();
      const controller = new AbortController();
      controllers.set(runId, controller);
      this.setTransport(runId, 'connecting');

      try {
        await connectRunEventStream({
          runId,
          threadId: current.threadId,
          token: authToken(token),
          after: current.lastSequence,
          signal: controller.signal,
          onOpen: () => this.setTransport(runId, 'open'),
          onReconnect: (attempt, _cursor, error) =>
            this.setTransport(runId, 'reconnecting', attempt, error),
          onEvent: (event) => this.apply(event),
          pauseWhen: (event) => event.type === 'hitl.required',
        });
        return this.byId[runId];
      } catch (error) {
        this.setTransport(runId, 'closed', 0, error);
        throw getPublicError(error);
      } finally {
        if (controllers.get(runId) === controller) {
          controllers.delete(runId);
          const state = this.byId[runId];
          if (state && state.transportStatus !== 'closed') {
            this.setTransport(runId, 'closed');
          }
        }
      }
    },

    async resume(runId: string, command: ResumeRunCommand): Promise<RunResumeResponse> {
      const current = this.byId[runId];
      if (!current) {
        throw getPublicError({
          code: 'NOT_FOUND',
          retryable: false,
          category: 'run',
        });
      }
      const existing = this.pendingResumes[runId];
      if (
        existing &&
        (existing.hitlToken !== command.hitlToken || existing.answer !== command.answer)
      ) {
        throw getPublicError({
          code: 'CONFLICT',
          retryable: false,
          category: 'run',
        });
      }
      if (
        existing &&
        command.idempotencyKey &&
        command.idempotencyKey !== existing.idempotencyKey
      ) {
        throw getPublicError({
          code: 'CONFLICT',
          retryable: false,
          category: 'run',
        });
      }
      const idempotencyKey =
        existing?.idempotencyKey || command.idempotencyKey || createIdempotencyKey('resume');
      this.pendingResumes[runId] = {
        hitlToken: command.hitlToken,
        answer: command.answer,
        idempotencyKey,
      };

      try {
        const response = await resumeRun(
          runId,
          {
            hitl_token: command.hitlToken,
            answer: command.answer,
            idempotency_key: idempotencyKey,
          },
          command.token
        );
        this.hydrate(runId, response.run);
        await this.connect(runId, command.token);
        return response;
      } catch (error) {
        throw getPublicError(error);
      }
    },

    async cancel(runId: string, token?: string): Promise<RunRecord> {
      const current = this.byId[runId];
      const previous = current
        ? {
            status: current.status,
            terminal: current.terminal,
            error: current.error,
          }
        : null;
      if (current && !current.terminal) current.status = 'cancelling';
      try {
        const response = await cancelRun(runId, authToken(token));
        this.hydrate(runId, response);
        return response;
      } catch (error) {
        if (current && previous && !current.terminal) {
          current.status = previous.status;
          current.terminal = previous.terminal;
          current.error = previous.error;
        }
        throw getPublicError(error);
      }
    },

    disconnect(runId: string) {
      controllersFor(this).get(runId)?.abort();
      controllersFor(this).delete(runId);
      this.setTransport(runId, 'closed');
    },

    disconnectAll() {
      const controllers = controllersFor(this);
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
      Object.keys(this.byId).forEach((runId) => this.setTransport(runId, 'closed'));
    },

    remove(runId: string) {
      this.disconnect(runId);
      delete this.pendingResumes[runId];
      delete this.byId[runId];
    },
  },
});
