// Generated from contracts/run_event_v1.json. Do not edit by hand.
export type RunEventType =
  | 'run.created'
  | 'run.started'
  | 'run.waiting_input'
  | 'run.completed'
  | 'run.failed'
  | 'run.cancelled'
  | 'planner.started'
  | 'planner.completed'
  | 'tool.started'
  | 'tool.progress'
  | 'tool.completed'
  | 'tool.failed'
  | 'tool.denied'
  | 'retrieval.started'
  | 'retrieval.candidates'
  | 'retrieval.rerank_completed'
  | 'retrieval.completed'
  | 'message.delta'
  | 'message.completed'
  | 'hitl.required'
  | 'hitl.resumed'
  | 'usage.updated'
  | 'artifact.created'
  | 'warning.created';

export interface RunEventV1<TData extends Record<string, unknown> = Record<string, unknown>> {
  schema_version: 1;
  event_id: string;
  sequence: number;
  run_id: string;
  thread_id: string;
  type: RunEventType;
  timestamp: string;
  data: TData;
}
