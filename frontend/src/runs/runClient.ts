import api from '@/utils/api';
import { requireThreadId } from '@/threads/threadId';
import type {
  RunCreateRequest,
  RunCreateResponse,
  RunEventsResponse,
  RunRecord,
  RunResumeRequest,
  RunResumeResponse,
} from '@/types/runs';

let fallbackKeySequence = 0;

export function createIdempotencyKey(scope: 'run' | 'resume' = 'run'): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === 'function') {
    return `${scope}_${randomUUID.call(globalThis.crypto)}`;
  }
  fallbackKeySequence += 1;
  const random = Math.random().toString(36).slice(2, 12);
  return `${scope}_${Date.now().toString(36)}_${fallbackKeySequence.toString(36)}_${random}`;
}

export async function createRun(
  threadId: string,
  request: RunCreateRequest
): Promise<RunCreateResponse> {
  const response = (
    await api.post<RunCreateResponse>(
      `/v1/threads/${encodeURIComponent(requireThreadId(threadId))}/runs`,
      request
    )
  ).data;
  requireThreadId(response.run.thread_id);
  return response;
}

export async function getRun(runId: string): Promise<RunRecord> {
  return (await api.get<RunRecord>(`/v1/runs/${encodeURIComponent(runId)}`)).data;
}

export async function getRunEvents(
  runId: string,
  options: { after?: number; limit?: number } = {}
): Promise<RunEventsResponse> {
  return (
    await api.get<RunEventsResponse>(`/v1/runs/${encodeURIComponent(runId)}/events`, {
      params: {
        after: Math.max(options.after || 0, 0),
        limit: Math.min(Math.max(options.limit || 500, 1), 1000),
      },
    })
  ).data;
}

export async function cancelRun(runId: string): Promise<RunRecord> {
  return (await api.post<RunRecord>(`/v1/runs/${encodeURIComponent(runId)}/cancel`)).data;
}

export async function resumeRun(
  runId: string,
  request: RunResumeRequest
): Promise<RunResumeResponse> {
  return (
    await api.post<RunResumeResponse>(`/v1/runs/${encodeURIComponent(runId)}/resume`, request)
  ).data;
}
