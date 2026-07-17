import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import { createThread, deleteThread, getThreadMessages, listThreads } from './threadClient';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: {
      delete: vi.fn(),
      get: vi.fn(),
      post: vi.fn(),
    },
  };
});

function threadSummary(threadId = 'thread_one') {
  return {
    thread_id: threadId,
    title: '第一条 Thread',
    message_count: 2,
    updated_at: '2026-07-16T08:00:00Z',
    version: 4,
    thread_status: 'active',
    active_run_id: null,
    active_run_status: null,
  };
}

describe('thread client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('creates a server-owned canonical Thread identity', async () => {
    const created = {
      ...threadSummary(),
      message_count: 0,
      version: 0,
      created_at: '2026-07-16T08:00:00Z',
    };
    vi.mocked(api.post).mockResolvedValue({ data: created });

    await expect(createThread({ title: '第一条 Thread' })).resolves.toEqual(created);

    expect(api.post).toHaveBeenCalledWith('/v1/threads', { title: '第一条 Thread' });
  });

  it('lists canonical Threads without using the legacy session route', async () => {
    const threads = [threadSummary()];
    vi.mocked(api.get).mockResolvedValue({ data: { threads } });

    await expect(listThreads()).resolves.toEqual(threads);

    expect(api.get).toHaveBeenCalledWith('/v1/threads');
  });

  it('reads the latest page and optional earlier cursor from the canonical path', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: {
        messages: [
          {
            id: 3,
            run_id: null,
            sequence: 3,
            status: 'completed',
            role: 'user',
            content: '问题',
            timestamp: '2026-07-16T08:00:00Z',
            rag_trace: null,
          },
        ],
        previous_cursor: 3,
      },
    });

    await getThreadMessages('thread_one', { before: 12, limit: 50 });

    expect(api.get).toHaveBeenCalledWith('/v1/threads/thread_one/messages', {
      params: { before: 12, limit: 50 },
    });
  });

  it('omits the before cursor when loading the latest page', async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { messages: [], previous_cursor: null },
    });

    await getThreadMessages('thread_one');

    expect(api.get).toHaveBeenCalledWith('/v1/threads/thread_one/messages', {
      params: { limit: 200 },
    });
  });

  it('rejects Thread IDs that cannot be represented by the backend path contract', async () => {
    await expect(getThreadMessages('thread/one')).rejects.toMatchObject({
      code: 'INVALID_REQUEST',
    });
    expect(api.get).not.toHaveBeenCalled();
  });

  it('deletes through the canonical Thread path', async () => {
    vi.mocked(api.delete).mockResolvedValue({
      data: { thread_id: 'thread_one', message: '成功删除 Thread' },
    });

    await expect(deleteThread('thread_one')).resolves.toEqual({
      thread_id: 'thread_one',
      message: '成功删除 Thread',
    });

    expect(api.delete).toHaveBeenCalledWith('/v1/threads/thread_one');
  });
});
