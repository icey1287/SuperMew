import type { RunEventV1 } from './generated/run-event-v1';
import type { PublicErrorInfo } from './publicError';

export type DurableRunStatus =
  | 'queued'
  | 'pending'
  | 'running'
  | 'waiting_input'
  | 'cancelling'
  | 'cancelled'
  | 'failed'
  | 'succeeded';

export interface RunRecord {
  id: string;
  thread_id: string;
  status: DurableRunStatus | string;
  idempotency_key: string;
  user_message_id: number;
  assistant_message_id: number;
  fencing_token?: number;
  on_disconnect: 'cancel' | 'continue' | string;
  error_code?: string | null;
  error?: PublicErrorInfo | null;
  created_at?: string;
  updated_at?: string;
}

export interface RunCreateRequest {
  message: string;
  idempotency_key: string;
  expected_thread_version?: number | null;
  multitask_strategy?: 'reject' | 'enqueue' | 'cancel_previous';
  on_disconnect?: 'cancel' | 'continue';
  approved_tools?: string[];
}

export interface RunCreateResponse {
  run: RunRecord;
  created: boolean;
  thread_version: number;
}

export interface RunResumeRequest {
  hitl_token: string;
  answer: string;
  idempotency_key: string;
}

export interface RunResumeResponse {
  run: RunRecord;
  checkpoint_id: string;
  created: boolean;
}

export interface RunEventsResponse {
  events: RunEventV1[];
  next_after: number;
}

export interface CreateRunCommand {
  threadId: string;
  message: string;
  token: string;
  idempotencyKey?: string;
  expectedThreadVersion?: number | null;
  multitaskStrategy?: 'reject' | 'enqueue' | 'cancel_previous';
  onDisconnect?: 'cancel' | 'continue';
  approvedTools?: string[];
}

export interface ResumeRunCommand {
  token: string;
  hitlToken: string;
  answer: string;
  idempotencyKey?: string;
}
