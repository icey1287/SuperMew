import type { RunEventType, RunEventV1 } from '@/types/generated/run-event-v1';
import { normalizePublicErrorInfo, type PublicErrorInfo } from '@/types/publicError';

type UnknownRecord = Record<string, unknown>;

export type RunLifecycleStatus =
  | 'idle'
  | 'creating'
  | 'queued'
  | 'pending'
  | 'running'
  | 'waiting_input'
  | 'cancelling'
  | 'cancelled'
  | 'failed'
  | 'completed';

export type RunTransportStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface RunHitlState {
  hitlToken: string | null;
  checkpointId: string | null;
  prompt: string;
  options: string[];
  route: string | null;
  retrievalStatus: string | null;
  originalQuestion: string | null;
}

export interface RunEventState {
  runId: string;
  threadId: string;
  idempotencyKey: string | null;
  status: RunLifecycleStatus;
  transportStatus: RunTransportStatus;
  reconnectAttempt: number;
  transportError: PublicErrorInfo | null;
  lastSequence: number;
  terminal: boolean;
  terminalSequence: number | null;
  hasGap: boolean;
  userMessageId: number | null;
  assistantMessageId: number | null;
  messageText: string;
  messageStatus: string | null;
  ragTrace: UnknownRecord | null;
  pendingHitl: RunHitlState | null;
  lastResumeAnswer: string | null;
  usage: UnknownRecord;
  toolProgress: Array<{
    toolName: string | null;
    step: UnknownRecord;
  }>;
  error: PublicErrorInfo | null;
  warnings: PublicErrorInfo[];
  toolFailures: Array<{
    toolName: string | null;
    error: PublicErrorInfo;
    fallbackApplied: boolean;
  }>;
  unknownEventTypes: string[];
}

export type RuntimeRunEvent = Omit<RunEventV1, 'type'> & {
  type: RunEventType | string;
};

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function safeString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function safeInteger(value: unknown): number | null {
  return Number.isInteger(value) && Number(value) > 0 ? Number(value) : null;
}

function lifecycleStatus(value: unknown): RunLifecycleStatus {
  const status = String(value || 'pending');
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
  return 'pending';
}

function eventError(data: UnknownRecord, defaults: Partial<PublicErrorInfo>): PublicErrorInfo {
  return normalizePublicErrorInfo(data, defaults);
}

function hitlState(data: UnknownRecord): RunHitlState {
  const rawOptions = Array.isArray(data.options) ? data.options : [];
  return {
    hitlToken: safeString(data.hitl_token),
    checkpointId: safeString(data.checkpoint_id),
    prompt: safeString(data.prompt) || '请补充一个关键信息后继续。',
    options: rawOptions
      .map((value) => safeString(value))
      .filter((value): value is string => value !== null),
    route: safeString(data.route),
    retrievalStatus: safeString(data.retrieval_status),
    originalQuestion: safeString(data.original_question),
  };
}

export function initialRunEventState(runId: string, threadId: string): RunEventState {
  return {
    runId,
    threadId,
    idempotencyKey: null,
    status: 'idle',
    transportStatus: 'idle',
    reconnectAttempt: 0,
    transportError: null,
    lastSequence: 0,
    terminal: false,
    terminalSequence: null,
    hasGap: false,
    userMessageId: null,
    assistantMessageId: null,
    messageText: '',
    messageStatus: null,
    ragTrace: null,
    pendingHitl: null,
    lastResumeAnswer: null,
    usage: {},
    toolProgress: [],
    error: null,
    warnings: [],
    toolFailures: [],
    unknownEventTypes: [],
  };
}

export function applyRunEvent(state: RunEventState, event: RuntimeRunEvent): RunEventState {
  if (
    event.run_id !== state.runId ||
    event.thread_id !== state.threadId ||
    event.sequence <= state.lastSequence ||
    state.terminalSequence !== null
  ) {
    return state;
  }

  if (event.sequence !== state.lastSequence + 1) {
    return state.hasGap ? state : { ...state, hasGap: true };
  }

  const next: RunEventState = {
    ...state,
    lastSequence: event.sequence,
  };
  const data = asRecord(event.data) || {};

  switch (event.type) {
    case 'run.created':
      next.status = lifecycleStatus(data.status);
      next.userMessageId = safeInteger(data.user_message_id);
      next.assistantMessageId = safeInteger(data.assistant_message_id);
      next.error = null;
      break;
    case 'run.started':
      next.status = 'running';
      next.error = null;
      break;
    case 'run.waiting_input':
      next.status = 'waiting_input';
      break;
    case 'run.completed':
      next.status = 'completed';
      next.terminal = true;
      next.terminalSequence = event.sequence;
      next.pendingHitl = null;
      next.error = null;
      break;
    case 'run.failed':
      next.status = 'failed';
      next.terminal = true;
      next.terminalSequence = event.sequence;
      next.pendingHitl = null;
      next.error = eventError(data, {
        code: 'RUN_EXECUTION_FAILED',
      });
      break;
    case 'run.cancelled':
      next.status = 'cancelled';
      next.terminal = true;
      next.terminalSequence = event.sequence;
      next.pendingHitl = null;
      next.error = eventError(data, {
        code: 'RUN_CANCELLED',
        retryable: false,
        category: 'run',
        stage: 'cancellation',
      });
      break;
    case 'message.delta':
      next.messageText += String(data.delta ?? data.content ?? '');
      next.messageStatus = 'streaming';
      break;
    case 'message.completed':
      next.messageText = String(data.content ?? next.messageText);
      next.messageStatus = String(data.status ?? 'completed');
      next.ragTrace = asRecord(data.rag_trace);
      break;
    case 'hitl.required':
      next.pendingHitl = hitlState(data);
      next.status = 'waiting_input';
      break;
    case 'hitl.resumed':
      next.pendingHitl = null;
      next.lastResumeAnswer = safeString(data.answer);
      next.status = 'running';
      break;
    case 'usage.updated':
      next.usage = { ...next.usage, ...data };
      break;
    case 'warning.created':
      if (data.code === 'CANCEL_REQUESTED') {
        next.status = 'cancelling';
      }
      next.warnings = [
        ...next.warnings,
        eventError(data, { code: 'INTERNAL_ERROR', retryable: false }),
      ];
      break;
    case 'tool.progress': {
      const step = asRecord(data.step);
      if (step) {
        next.toolProgress = [
          ...next.toolProgress,
          {
            toolName: safeString(data.tool_name),
            step,
          },
        ];
      }
      break;
    }
    case 'tool.failed':
    case 'tool.denied':
      next.toolFailures = [
        ...next.toolFailures,
        {
          toolName: safeString(data.tool_name),
          error: eventError(data, {
            code: event.type === 'tool.denied' ? 'POLICY_DENIED' : 'TOOL_EXECUTION_FAILED',
            retryable: false,
            stage: 'tool',
          }),
          fallbackApplied: data.fallback_applied === true,
        },
      ];
      break;
    case 'planner.started':
    case 'planner.completed':
    case 'tool.started':
    case 'tool.completed':
    case 'retrieval.started':
    case 'retrieval.candidates':
    case 'retrieval.rerank_completed':
    case 'retrieval.completed':
    case 'artifact.created':
      break;
    default:
      next.unknownEventTypes = [...next.unknownEventTypes, String(event.type)];
  }
  return next;
}
