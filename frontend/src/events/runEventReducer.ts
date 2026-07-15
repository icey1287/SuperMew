import type { RunEventType, RunEventV1 } from '@/types/generated/run-event-v1';
import {
  normalizePublicErrorInfo,
  type PublicErrorInfo,
} from '@/types/publicError';

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
  | 'completed'
  | 'reconnecting';

export interface RunEventState {
  runId: string;
  threadId: string;
  status: RunLifecycleStatus;
  lastSequence: number;
  terminal: boolean;
  hasGap: boolean;
  messageText: string;
  messageStatus: string | null;
  pendingHitl: Record<string, unknown> | null;
  usage: Record<string, unknown>;
  error: PublicErrorInfo | null;
  warnings: PublicErrorInfo[];
  toolFailures: Array<{
    toolName: string | null;
    error: PublicErrorInfo;
    fallbackApplied: boolean;
  }>;
  unknownEventTypes: string[];
}

type RuntimeEvent = Omit<RunEventV1, 'type'> & { type: RunEventType | string };

export function initialRunEventState(runId: string, threadId: string): RunEventState {
  return {
    runId,
    threadId,
    status: 'idle',
    lastSequence: 0,
    terminal: false,
    hasGap: false,
    messageText: '',
    messageStatus: null,
    pendingHitl: null,
    usage: {},
    error: null,
    warnings: [],
    toolFailures: [],
    unknownEventTypes: [],
  };
}

function eventError(
  data: Record<string, unknown>,
  defaults: Partial<PublicErrorInfo>
): PublicErrorInfo {
  return normalizePublicErrorInfo(data, defaults);
}

export function applyRunEvent(state: RunEventState, event: RuntimeEvent): RunEventState {
  if (event.run_id !== state.runId || event.sequence <= state.lastSequence) {
    return state;
  }

  const next: RunEventState = {
    ...state,
    lastSequence: event.sequence,
    hasGap: state.hasGap || (state.lastSequence > 0 && event.sequence !== state.lastSequence + 1),
  };
  const data = event.data || {};

  if (state.terminal && event.type === 'message.delta') {
    return next;
  }

  switch (event.type) {
    case 'run.created':
      next.status = String(data.status || 'pending') as RunLifecycleStatus;
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
      next.error = null;
      break;
    case 'run.failed':
      next.status = 'failed';
      next.terminal = true;
      next.error = eventError(data, {
        code: 'RUN_EXECUTION_FAILED',
      });
      break;
    case 'run.cancelled':
      next.status = 'cancelled';
      next.terminal = true;
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
      break;
    case 'hitl.required':
      next.pendingHitl = { ...data };
      next.status = 'waiting_input';
      break;
    case 'hitl.resumed':
      next.pendingHitl = null;
      next.status = 'running';
      break;
    case 'usage.updated':
      next.usage = { ...next.usage, ...data };
      break;
    case 'warning.created':
      next.warnings = [
        ...next.warnings,
        eventError(data, { code: 'INTERNAL_ERROR', retryable: false }),
      ];
      break;
    case 'planner.started':
    case 'planner.completed':
    case 'tool.started':
    case 'tool.progress':
    case 'tool.completed':
      break;
    case 'tool.failed':
      next.toolFailures = [
        ...next.toolFailures,
        {
          toolName: typeof data.tool_name === 'string' ? data.tool_name : null,
          error: eventError(data, {
            code: 'TOOL_EXECUTION_FAILED',
            retryable: false,
            stage: 'tool',
          }),
          fallbackApplied: data.fallback_applied === true,
        },
      ];
      break;
    case 'tool.denied':
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
