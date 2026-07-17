import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createThread, deleteThread, listThreads } from '@/threads/threadClient';
import { useSessionStore } from './sessions';

vi.mock('@/threads/threadClient', () => ({
  createThread: vi.fn(),
  deleteThread: vi.fn(),
  listThreads: vi.fn(),
}));

function threadSummary(threadId = 'thread-1', activeRunStatus: string | null = null) {
  return {
    thread_id: threadId,
    title: '历史 Thread',
    message_count: 2,
    updated_at: '2026-07-16T08:00:00Z',
    version: 4,
    thread_status: 'active',
    active_run_id: activeRunStatus ? 'run-1' : null,
    active_run_status: activeRunStatus,
  };
}

describe('Thread history store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('loads canonical Thread summaries into a separate list view model', async () => {
    vi.mocked(listThreads).mockResolvedValue([threadSummary('thread-1', 'waiting_input')]);
    const store = useSessionStore();

    await store.fetchSessions();

    expect(store.sessions[0]).toMatchObject({
      thread_id: 'thread-1',
      thread_status: 'active',
      active_run_status: 'waiting_input',
      activeRunId: 'run-1',
      activeRunStatus: 'waiting_input',
      isStreaming: true,
    });
    expect(listThreads).toHaveBeenCalledOnce();
  });

  it('creates a Thread through the server-owned identity interface', async () => {
    vi.mocked(createThread).mockResolvedValue({
      ...threadSummary('thread-created'),
      message_count: 0,
      version: 0,
      created_at: '2026-07-16T08:00:00Z',
    });
    const store = useSessionStore();

    await expect(store.createSession('新的 Thread')).resolves.toMatchObject({
      thread_id: 'thread-created',
      isStreaming: false,
    });

    expect(createThread).toHaveBeenCalledWith({ title: '新的 Thread' });
    expect(store.sessions[0].thread_id).toBe('thread-created');
  });

  it('keeps Run projection separate from canonical Thread status', async () => {
    vi.mocked(listThreads).mockResolvedValue([threadSummary()]);
    const store = useSessionStore();
    await store.fetchSessions();

    store.setRunView('thread-1', 'run-1', 'running');

    expect(store.sessions[0]).toMatchObject({
      thread_status: 'active',
      active_run_status: null,
      activeRunId: 'run-1',
      activeRunStatus: 'running',
      isStreaming: true,
    });
  });

  it('removes only the deleted Thread after canonical deletion succeeds', async () => {
    vi.mocked(deleteThread).mockResolvedValue({
      thread_id: 'thread-1',
      message: '成功删除 Thread',
    });
    const store = useSessionStore();
    store.sessions = [
      {
        ...threadSummary('thread-1'),
        activeRunId: null,
        activeRunStatus: null,
        isStreaming: false,
      },
      {
        ...threadSummary('thread-2'),
        activeRunId: null,
        activeRunStatus: null,
        isStreaming: false,
      },
    ];

    await expect(store.deleteSession('thread-1')).resolves.toBe('成功删除 Thread');

    expect(deleteThread).toHaveBeenCalledWith('thread-1');
    expect(store.sessions.map((thread) => thread.thread_id)).toEqual(['thread-2']);
  });
});
