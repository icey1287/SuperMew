import type {
  ThreadCreateRequest,
  ThreadDeleteResult,
  ThreadDetail,
  ThreadMessagePage,
  ThreadSummary,
} from '@/types/threads';
import { requireThreadId } from '@/threads/threadId';
import api from '@/utils/api';

type ThreadListResponse = {
  threads: ThreadSummary[];
};

export async function createThread(request: ThreadCreateRequest = {}): Promise<ThreadDetail> {
  const response = await api.post<ThreadDetail>('/v1/threads', request);
  requireThreadId(response.data.thread_id);
  return response.data;
}

export async function listThreads(): Promise<ThreadSummary[]> {
  const response = await api.get<ThreadListResponse>('/v1/threads');
  return (response.data.threads || []).map((thread) => {
    requireThreadId(thread.thread_id);
    return thread;
  });
}

export async function getThreadMessages(
  threadId: string,
  options: { before?: number; limit?: number } = {}
): Promise<ThreadMessagePage> {
  const params: { before?: number; limit: number } = {
    limit: options.limit ?? 200,
  };
  if (options.before !== undefined) params.before = options.before;
  const response = await api.get<ThreadMessagePage>(
    `/v1/threads/${encodeURIComponent(requireThreadId(threadId))}/messages`,
    {
      params,
    }
  );
  return response.data;
}

export async function deleteThread(threadId: string): Promise<ThreadDeleteResult> {
  const response = await api.delete<ThreadDeleteResult>(
    `/v1/threads/${encodeURIComponent(requireThreadId(threadId))}`
  );
  return response.data;
}
