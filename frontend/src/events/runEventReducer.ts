import type { RunEventType, RunEventV1 } from '@/types/generated/run-event-v1';

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
  warnings: string[];
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
    warnings: [],
    unknownEventTypes: [],
  };
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
      break;
    case 'run.started':
      next.status = 'running';
      break;
    case 'run.waiting_input':
      next.status = 'waiting_input';
      break;
    case 'run.completed':
      next.status = 'completed';
      next.terminal = true;
      break;
    case 'run.failed':
      next.status = 'failed';
      next.terminal = true;
      break;
    case 'run.cancelled':
      next.status = 'cancelled';
      next.terminal = true;
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
      next.warnings = [...next.warnings, String(data.message ?? data.code ?? 'warning')];
      break;
    case 'planner.started':
    case 'planner.completed':
    case 'tool.started':
    case 'tool.progress':
    case 'tool.completed':
    case 'tool.failed':
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
