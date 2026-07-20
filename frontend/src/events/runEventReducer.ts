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

export type RunTimelineKind = 'run' | 'planner' | 'retrieval' | 'tool' | 'artifact' | 'warning';

export type RunTimelineStatus =
  'pending' | 'running' | 'completed' | 'failed' | 'denied' | 'warning';

export interface RunTimelineItem {
  id: string;
  sequence: number;
  kind: RunTimelineKind;
  eventType: string;
  status: RunTimelineStatus;
  title: string;
  detail: string | null;
  timestamp: string;
  toolName: string | null;
  toolCallId: string | null;
  durationMs: number | null;
  resultSize: number | null;
  guardrailDecision: string | null;
  guardrailReasonCode: string | null;
  error: PublicErrorInfo | null;
}

export interface RunArtifactState {
  artifactId: string;
  name: string;
  mediaType: string;
  uri: string | null;
  sizeBytes: number | null;
  sha256: string | null;
  toolName: string | null;
  toolCallId: string | null;
  sequence: number;
  createdAt: string;
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
  activeDurationMs: number;
  activeStartedAt: string | null;
  hasGap: boolean;
  userMessageId: number | null;
  assistantMessageId: number | null;
  messageText: string;
  messageStatus: string | null;
  ragTrace: UnknownRecord | null;
  pendingHitl: RunHitlState | null;
  lastResumeAnswer: string | null;
  usage: UnknownRecord;
  timeline: RunTimelineItem[];
  artifacts: RunArtifactState[];
  activeSkillName: string | null;
  activeSkillVersion: string | null;
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

function safeNonNegativeInteger(value: unknown): number | null {
  return Number.isInteger(value) && Number(value) >= 0 ? Number(value) : null;
}

function timelineDetail(data: UnknownRecord): string | null {
  return (
    safeString(data.message) ||
    safeString(data.detail) ||
    safeString(asRecord(data.step)?.detail) ||
    safeString(asRecord(data.step)?.label)
  );
}

function toolTimelineId(data: UnknownRecord, sequence: number): string {
  return `tool:${safeString(data.tool_call_id) || safeString(data.tool_name) || sequence}`;
}

function replaceTimelineItem(next: RunEventState, item: RunTimelineItem): void {
  const existingIndex = next.timeline.findIndex((candidate) => candidate.id === item.id);
  if (existingIndex < 0) {
    next.timeline = [...next.timeline, item];
    return;
  }
  const timeline = [...next.timeline];
  timeline[existingIndex] = { ...timeline[existingIndex], ...item };
  next.timeline = timeline;
}

function appendTimelineItem(next: RunEventState, item: RunTimelineItem): void {
  next.timeline = [...next.timeline, item];
}

function baseTimelineItem(
  event: RuntimeRunEvent,
  data: UnknownRecord,
  values: Pick<RunTimelineItem, 'id' | 'kind' | 'status' | 'title'>
): RunTimelineItem {
  return {
    ...values,
    sequence: event.sequence,
    eventType: String(event.type),
    detail: timelineDetail(data),
    timestamp: event.timestamp,
    toolName: safeString(data.tool_name),
    toolCallId: safeString(data.tool_call_id),
    durationMs: safeNonNegativeInteger(data.duration_ms),
    resultSize: safeNonNegativeInteger(data.result_size),
    guardrailDecision: safeString(data.guardrail_decision),
    guardrailReasonCode: safeString(data.reason_code) || safeString(data.guardrail_reason_code),
    error: null,
  };
}

function artifactState(event: RuntimeRunEvent, data: UnknownRecord): RunArtifactState | null {
  const artifactId = safeString(data.artifact_id);
  const name = safeString(data.name);
  const mediaType = safeString(data.media_type);
  if (!artifactId || !name || !mediaType) return null;
  return {
    artifactId,
    name,
    mediaType,
    uri: safeString(data.uri),
    sizeBytes: safeNonNegativeInteger(data.size_bytes),
    sha256: safeString(data.sha256),
    toolName: safeString(data.tool_name),
    toolCallId: safeString(data.tool_call_id),
    sequence: event.sequence,
    createdAt: event.timestamp,
  };
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

function eventTimestamp(value: string): number | null {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function finishActiveInterval(state: RunEventState, endedAt: string): void {
  const started = state.activeStartedAt ? eventTimestamp(state.activeStartedAt) : null;
  const ended = eventTimestamp(endedAt);
  if (started !== null && ended !== null && ended >= started) {
    state.activeDurationMs += ended - started;
  }
  state.activeStartedAt = null;
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
    activeDurationMs: 0,
    activeStartedAt: null,
    hasGap: false,
    userMessageId: null,
    assistantMessageId: null,
    messageText: '',
    messageStatus: null,
    ragTrace: null,
    pendingHitl: null,
    lastResumeAnswer: null,
    usage: {},
    timeline: [],
    artifacts: [],
    activeSkillName: null,
    activeSkillVersion: null,
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
      appendTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: `run:created:${event.sequence}`,
          kind: 'run',
          status: 'pending',
          title: 'Run 已创建',
        })
      );
      break;
    case 'run.started':
      next.status = 'running';
      next.error = null;
      if (next.activeStartedAt === null) next.activeStartedAt = event.timestamp;
      appendTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: `run:started:${event.sequence}`,
          kind: 'run',
          status: 'running',
          title: '开始执行',
        })
      );
      break;
    case 'run.waiting_input':
      finishActiveInterval(next, event.timestamp);
      next.status = 'waiting_input';
      break;
    case 'run.completed':
      finishActiveInterval(next, event.timestamp);
      next.status = 'completed';
      next.terminal = true;
      next.terminalSequence = event.sequence;
      next.pendingHitl = null;
      next.error = null;
      appendTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: `run:terminal:${event.sequence}`,
          kind: 'run',
          status: 'completed',
          title: 'Run 已完成',
        })
      );
      break;
    case 'run.failed':
      finishActiveInterval(next, event.timestamp);
      next.status = 'failed';
      next.terminal = true;
      next.terminalSequence = event.sequence;
      next.pendingHitl = null;
      next.error = eventError(data, {
        code: 'RUN_EXECUTION_FAILED',
      });
      appendTimelineItem(next, {
        ...baseTimelineItem(event, data, {
          id: `run:terminal:${event.sequence}`,
          kind: 'run',
          status: 'failed',
          title: 'Run 执行失败',
        }),
        error: next.error,
      });
      break;
    case 'run.cancelled':
      finishActiveInterval(next, event.timestamp);
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
      appendTimelineItem(next, {
        ...baseTimelineItem(event, data, {
          id: `run:terminal:${event.sequence}`,
          kind: 'run',
          status: 'failed',
          title: 'Run 已取消',
        }),
        error: next.error,
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
      next.messageText = '';
      next.messageStatus = 'waiting_input';
      next.pendingHitl = hitlState(data);
      next.status = 'waiting_input';
      break;
    case 'hitl.resumed':
      next.pendingHitl = null;
      next.lastResumeAnswer = safeString(data.answer);
      next.messageStatus = 'streaming';
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
      appendTimelineItem(next, {
        ...baseTimelineItem(event, data, {
          id: `warning:${event.sequence}`,
          kind: 'warning',
          status: 'warning',
          title: safeString(data.message) || '执行警告',
        }),
        error: next.warnings[next.warnings.length - 1],
      });
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
        replaceTimelineItem(
          next,
          baseTimelineItem(event, data, {
            id: toolTimelineId(data, event.sequence),
            kind: 'tool',
            status: 'running',
            title: safeString(step.label) || `${safeString(data.tool_name) || '工具'}执行中`,
          })
        );
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
      replaceTimelineItem(next, {
        ...baseTimelineItem(event, data, {
          id: toolTimelineId(data, event.sequence),
          kind: 'tool',
          status: event.type === 'tool.denied' ? 'denied' : 'failed',
          title:
            event.type === 'tool.denied'
              ? `${safeString(data.tool_name) || '工具'}未获允许`
              : `${safeString(data.tool_name) || '工具'}执行失败`,
        }),
        error: next.toolFailures[next.toolFailures.length - 1].error,
      });
      break;
    case 'planner.started':
      replaceTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: 'planner',
          kind: 'planner',
          status: 'running',
          title: '正在规划执行路径',
        })
      );
      break;
    case 'planner.completed':
      replaceTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: 'planner',
          kind: 'planner',
          status: 'completed',
          title: '执行路径已确定',
        })
      );
      break;
    case 'tool.started':
      replaceTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: toolTimelineId(data, event.sequence),
          kind: 'tool',
          status: 'running',
          title: `${safeString(data.tool_name) || '工具'}开始执行`,
        })
      );
      break;
    case 'tool.completed':
      replaceTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: toolTimelineId(data, event.sequence),
          kind: 'tool',
          status: 'completed',
          title: `${safeString(data.tool_name) || '工具'}执行完成`,
        })
      );
      break;
    case 'retrieval.started':
    case 'retrieval.candidates':
    case 'retrieval.rerank_completed':
      replaceTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: 'retrieval',
          kind: 'retrieval',
          status: 'running',
          title:
            event.type === 'retrieval.started'
              ? '开始检索证据'
              : event.type === 'retrieval.candidates'
                ? '正在整理候选证据'
                : '证据精排已完成',
        })
      );
      break;
    case 'retrieval.completed':
      replaceTimelineItem(
        next,
        baseTimelineItem(event, data, {
          id: 'retrieval',
          kind: 'retrieval',
          status: 'completed',
          title: '证据检索已完成',
        })
      );
      break;
    case 'artifact.created': {
      const artifact = artifactState(event, data);
      if (artifact) {
        const withoutExisting = next.artifacts.filter(
          (candidate) => candidate.artifactId !== artifact.artifactId
        );
        next.artifacts = [...withoutExisting, artifact];
        appendTimelineItem(
          next,
          baseTimelineItem(event, data, {
            id: `artifact:${artifact.artifactId}`,
            kind: 'artifact',
            status: 'completed',
            title: `生成 Artifact：${artifact.name}`,
          })
        );
      }
      break;
    }
    default:
      next.unknownEventTypes = [...next.unknownEventTypes, String(event.type)];
  }
  return next;
}
