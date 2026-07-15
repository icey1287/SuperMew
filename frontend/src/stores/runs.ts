import { defineStore } from 'pinia';
import api from '@/utils/api';
import {
  applyRunEvent,
  initialRunEventState,
  type RunEventState,
} from '@/events/runEventReducer';
import type { RunEventV1 } from '@/types/generated/run-event-v1';
import { getPublicError } from '@/utils/api';
import { normalizePublicErrorInfo } from '@/types/publicError';

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function lifecycleStatus(value: unknown): RunEventState['status'] | null {
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
      'reconnecting',
    ].includes(status)
  ) {
    return status as RunEventState['status'];
  }
  return null;
}

export const useRunsStore = defineStore('runs', {
  state: () => ({
    byId: {} as Record<string, RunEventState>,
  }),
  getters: {
    activeForThread: (state) => (threadId: string | null) => {
      if (!threadId) return null;
      return Object.values(state.byId).find(
        (run) =>
          run.threadId === threadId &&
          !run.terminal &&
          !['idle', 'cancelled', 'failed', 'completed'].includes(run.status)
      ) || null;
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
    apply(event: RunEventV1) {
      const current = this.ensure(event.run_id, event.thread_id);
      this.byId[event.run_id] = applyRunEvent(current, event);
    },
    hydrate(runId: string, payload: unknown): RunEventState | null {
      const data = asRecord(payload);
      if (!data) return this.byId[runId] || null;
      const threadId = typeof data.thread_id === 'string' ? data.thread_id : '';
      const current = this.byId[runId] || (threadId ? this.ensure(runId, threadId) : null);
      if (!current) return null;

      const status = lifecycleStatus(data.status) || current.status;
      const incomingTerminal = ['completed', 'failed', 'cancelled'].includes(status);
      if (current.terminal && (!incomingTerminal || status !== current.status)) {
        return current;
      }
      current.status = status;
      current.terminal = incomingTerminal;

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
    remove(runId: string) {
      delete this.byId[runId];
    },
    async cancel(runId: string) {
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
        const response = await api.post(`/v1/runs/${encodeURIComponent(runId)}/cancel`);
        this.hydrate(runId, response.data);
        return response.data;
      } catch (error) {
        if (current && previous && !current.terminal) {
          current.status = previous.status;
          current.terminal = previous.terminal;
          current.error = previous.error;
        }
        throw getPublicError(error);
      }
    },
  },
});
