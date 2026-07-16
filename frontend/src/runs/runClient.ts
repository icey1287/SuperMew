import api, { getPublicError } from '@/utils/api';
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

function authorization(token: string) {
  return {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  };
}

async function publicRequest<T>(request: Promise<{ data: T }>): Promise<T> {
  try {
    return (await request).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export function createRun(
  threadId: string,
  request: RunCreateRequest,
  token: string
): Promise<RunCreateResponse> {
  return publicRequest(
    api.post<RunCreateResponse>(
      `/v1/threads/${encodeURIComponent(threadId)}/runs`,
      request,
      authorization(token)
    )
  );
}

export function getRun(runId: string, token: string): Promise<RunRecord> {
  return publicRequest(
    api.get<RunRecord>(`/v1/runs/${encodeURIComponent(runId)}`, authorization(token))
  );
}

export function getRunEvents(
  runId: string,
  token: string,
  options: { after?: number; limit?: number } = {}
): Promise<RunEventsResponse> {
  return publicRequest(
    api.get<RunEventsResponse>(`/v1/runs/${encodeURIComponent(runId)}/events`, {
      ...authorization(token),
      params: {
        after: Math.max(options.after || 0, 0),
        limit: Math.min(Math.max(options.limit || 500, 1), 1000),
      },
    })
  );
}

export function cancelRun(runId: string, token: string): Promise<RunRecord> {
  return publicRequest(
    api.post<RunRecord>(
      `/v1/runs/${encodeURIComponent(runId)}/cancel`,
      undefined,
      authorization(token)
    )
  );
}

export function resumeRun(
  runId: string,
  request: RunResumeRequest,
  token: string
): Promise<RunResumeResponse> {
  return publicRequest(
    api.post<RunResumeResponse>(
      `/v1/runs/${encodeURIComponent(runId)}/resume`,
      request,
      authorization(token)
    )
  );
}
