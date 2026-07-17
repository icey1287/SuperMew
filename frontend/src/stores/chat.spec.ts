import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clearAuthSession, installAuthSession } from '@/auth/session';
import type { RuntimeRunEvent } from '@/events/runEventReducer';
import { connectRunEventStream } from '@/events/runEventStream';
import { cancelRun, createRun, getRun, getRunEvents, resumeRun } from '@/runs/runClient';
import { createThread, deleteThread, getThreadMessages } from '@/threads/threadClient';
import { useAuthStore } from './auth';
import { useChatStore } from './chat';
import { useRunsStore } from './runs';
import { useSessionStore } from './sessions';

vi.mock('@/runs/runClient', () => ({
  cancelRun: vi.fn(),
  createIdempotencyKey: vi.fn((scope: string) => `${scope}_stable_key`),
  createRun: vi.fn(),
  getRun: vi.fn(),
  getRunEvents: vi.fn(),
  resumeRun: vi.fn(),
}));

vi.mock('@/events/runEventStream', () => ({
  connectRunEventStream: vi.fn(),
}));

vi.mock('@/threads/threadClient', () => ({
  createThread: vi.fn(),
  deleteThread: vi.fn(),
  getThreadMessages: vi.fn(),
  listThreads: vi.fn(),
}));

type StreamOptions = Parameters<typeof connectRunEventStream>[0];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function event(
  runId: string,
  threadId: string,
  sequence: number,
  type: RuntimeRunEvent['type'],
  data: Record<string, unknown> = {}
): RuntimeRunEvent {
  return {
    schema_version: 1,
    event_id: `evt_${runId}_${sequence}`,
    sequence,
    run_id: runId,
    thread_id: threadId,
    type,
    timestamp: '2026-07-16T00:00:00Z',
    data,
  };
}

function runRecord(runId = 'run_1', threadId = 'thread-1', status = 'pending') {
  return {
    id: runId,
    thread_id: threadId,
    status,
    idempotency_key: 'run_stable_key',
    request_hash: 'hash',
    multitask_strategy: 'reject',
    fencing_token: 1,
    user_message_id: Number(runId.replace(/\D/g, '') || '1') * 10 + 1,
    assistant_message_id: Number(runId.replace(/\D/g, '') || '1') * 10 + 2,
    model_name: 'test-model',
    on_disconnect: 'continue',
    input_tokens: 0,
    output_tokens: 0,
    cost: '0',
    created_at: '2026-07-16T00:00:00Z',
    updated_at: '2026-07-16T00:00:00Z',
  } as any;
}

function createResponse(runId = 'run_1', threadId = 'thread-1') {
  return {
    run: runRecord(runId, threadId),
    created: true,
    thread_version: 2,
  };
}

function threadDetail(threadId = 'thread-1') {
  return {
    thread_id: threadId,
    title: '新对话',
    message_count: 0,
    version: 0,
    thread_status: 'active',
    active_run_id: null,
    active_run_status: null,
    created_at: '2026-07-16T00:00:00Z',
    updated_at: '2026-07-16T00:00:00Z',
  };
}

function threadListItem(threadId = 'thread-1') {
  return {
    ...threadDetail(threadId),
    activeRunId: null,
    activeRunStatus: null,
    isStreaming: false,
  };
}

function threadMessage(
  sequence: number,
  role: 'user' | 'assistant' | 'system',
  content: string,
  overrides: Record<string, unknown> = {}
) {
  return {
    id: sequence,
    run_id: null,
    sequence,
    status: 'completed',
    role,
    content,
    timestamp: `2026-07-16T00:00:${String(sequence).padStart(2, '0')}Z`,
    rag_trace: null,
    ...overrides,
  } as any;
}

function createLocalStorageMock() {
  const values = new Map<string, string>();
  return {
    getItem: vi.fn((key: string) => values.get(key) || null),
    setItem: vi.fn((key: string, value: string) => values.set(key, value)),
    removeItem: vi.fn((key: string) => values.delete(key)),
    clear: vi.fn(() => values.clear()),
  };
}

function setupStores(threadId = 'thread-1') {
  setActivePinia(createPinia());
  installAuthSession({
    access_token: 'test-token',
    username: 'tester',
    role: 'user',
  });
  const authStore = useAuthStore();
  const chatStore = useChatStore();
  const sessionStore = useSessionStore();
  if (threadId) sessionStore.sessions = [threadListItem(threadId)];
  chatStore.setViewedSession(threadId, []);
  return {
    authStore,
    chatStore,
    runsStore: useRunsStore(),
    sessionStore,
  };
}

function installControlledStreams() {
  const connections = new Map<
    string,
    { options: StreamOptions; result: ReturnType<typeof deferred<number>> }
  >();
  vi.mocked(connectRunEventStream).mockImplementation((options) => {
    const result = deferred<number>();
    connections.set(options.runId, { options, result });
    options.onOpen?.(options.after || 0);
    return result.promise;
  });
  return {
    connections,
    emit(runId: string, item: RuntimeRunEvent) {
      connections.get(runId)?.options.onEvent(item);
    },
    finish(runId: string, sequence: number) {
      connections.get(runId)?.result.resolve(sequence);
    },
  };
}

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('durable chat projection', () => {
  beforeEach(() => {
    clearAuthSession();
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', createLocalStorageMock());
    vi.stubGlobal('alert', vi.fn());
    vi.stubGlobal(
      'confirm',
      vi.fn(() => true)
    );
    vi.mocked(createThread).mockResolvedValue(threadDetail());
    vi.mocked(getThreadMessages).mockResolvedValue({
      messages: [],
      previous_cursor: null,
    });
  });

  it('clears account-scoped messages, Run state, and local stream ownership', () => {
    const { chatStore, runsStore } = setupStores();
    chatStore.messagesBySession['thread-1'] = [{ text: '旧账号消息', isUser: true }];
    chatStore.messages = chatStore.messagesBySession['thread-1'];
    chatStore.userInput = '草稿';
    runsStore.ensure('run_1', 'thread-1').status = 'running';

    chatStore.resetWorkspace();

    expect(chatStore.messagesBySession).toEqual({});
    expect(chatStore.messages).toEqual([]);
    expect(chatStore.userInput).toBe('');
    expect(runsStore.byId).toEqual({});
  });

  it('creates a server-owned Thread before the first durable Run', async () => {
    const streams = installControlledStreams();
    vi.mocked(createThread).mockResolvedValue(threadDetail('thread-server'));
    vi.mocked(createRun).mockResolvedValue(createResponse('run_1', 'thread-server'));
    const { chatStore } = setupStores('');
    chatStore.userInput = '第一条问题';

    const sending = chatStore.handleSend();
    await flushPromises();

    expect(createThread).toHaveBeenCalledWith({ title: '第一条问题' });
    expect(createRun).toHaveBeenCalledWith(
      'thread-server',
      expect.objectContaining({ expected_thread_version: 0 }),
      'test-token'
    );
    expect(chatStore.sessionId).toBe('thread-server');
    expect(chatStore.sessionId).not.toMatch(/^session_/);

    streams.emit('run_1', event('run_1', 'thread-server', 1, 'run.created'));
    streams.emit(
      'run_1',
      event('run_1', 'thread-server', 2, 'message.completed', { content: '第一条回答' })
    );
    streams.emit('run_1', event('run_1', 'thread-server', 3, 'run.completed'));
    streams.finish('run_1', 3);
    await sending;
  });

  it('authoritatively deletes the current Thread before creating a replacement', async () => {
    vi.mocked(deleteThread).mockResolvedValue({
      thread_id: 'thread-1',
      message: '成功删除 Thread',
    });
    vi.mocked(createThread).mockResolvedValue(threadDetail('thread-replacement'));
    const { chatStore, sessionStore } = setupStores();
    chatStore.messagesBySession['thread-1'] = [{ text: '旧消息', isUser: true }];
    chatStore.messages = chatStore.messagesBySession['thread-1'];

    await chatStore.handleClearChat();

    expect(deleteThread).toHaveBeenCalledWith('thread-1');
    expect(createThread).toHaveBeenCalledWith({});
    expect(chatStore.sessionId).toBe('thread-replacement');
    expect(chatStore.messagesBySession['thread-1']).toBeUndefined();
    expect(sessionStore.sessions.map((thread) => thread.thread_id)).toEqual(['thread-replacement']);
  });

  it('keeps the current Thread intact when authoritative deletion reports an active Run', async () => {
    vi.mocked(deleteThread).mockRejectedValue({
      code: 'RUN_ACTIVE',
      message: 'Thread 仍有活跃 Run',
      retryable: false,
      category: 'thread',
    });
    const { chatStore, sessionStore } = setupStores();
    chatStore.messagesBySession['thread-1'] = [{ text: '仍需保留', isUser: true }];
    chatStore.messages = chatStore.messagesBySession['thread-1'];

    await chatStore.handleClearChat();

    expect(createThread).not.toHaveBeenCalled();
    expect(chatStore.sessionId).toBe('thread-1');
    expect(chatStore.messages[0].text).toBe('仍需保留');
    expect(sessionStore.sessions[0].thread_id).toBe('thread-1');
    expect(alert).toHaveBeenCalledWith('Thread 仍有活跃 Run');
  });

  it('locks input from canonical active Run metadata until the Run is restored', () => {
    const { chatStore, sessionStore } = setupStores();
    sessionStore.sessions[0].active_run_id = 'run-server';
    sessionStore.sessions[0].active_run_status = 'running';
    sessionStore.sessions[0].activeRunId = 'run-server';
    sessionStore.sessions[0].activeRunStatus = 'running';
    sessionStore.sessions[0].isStreaming = true;

    expect(chatStore.currentRunStatus).toBe('running');
    expect(chatStore.isInputLocked).toBe(true);
    expect(chatStore.isViewingStreamingSession).toBe(false);
  });

  it('creates optimistic messages, reserves a durable Run, and projects final authority', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse());
    const { chatStore, sessionStore } = setupStores();
    chatStore.userInput = '帮我总结文档';

    const sending = chatStore.handleSend();
    await flushPromises();

    expect(chatStore.messages).toHaveLength(2);
    expect(chatStore.messages[0]).toMatchObject({
      id: 11,
      runId: 'run_1',
      text: '帮我总结文档',
      isUser: true,
    });
    expect(chatStore.messages[1]).toMatchObject({
      id: 12,
      runId: 'run_1',
      isThinking: true,
    });
    expect(sessionStore.sessions[0]).toMatchObject({
      thread_id: 'thread-1',
      isStreaming: true,
    });
    expect(createRun).toHaveBeenCalledWith(
      'thread-1',
      expect.objectContaining({
        message: '帮我总结文档',
        multitask_strategy: 'reject',
        on_disconnect: 'continue',
        approved_tools: [],
      }),
      'test-token'
    );

    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 1, 'run.created', {
        status: 'pending',
        user_message_id: 11,
        assistant_message_id: 12,
      })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 2, 'run.started'));
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 3, 'message.delta', {
        content: '临时片段',
      })
    );
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 4, 'message.completed', {
        content: '最终回答',
        status: 'completed',
        rag_trace: { retrieval_outcome: 'ANSWERABLE' },
      })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 5, 'run.completed'));
    streams.finish('run_1', 5);
    await sending;

    expect(chatStore.messages[1]).toMatchObject({
      text: '最终回答',
      status: 'completed',
      isThinking: false,
      ragTrace: { retrieval_outcome: 'ANSWERABLE' },
    });
    expect(sessionStore.sessions[0].isStreaming).toBe(false);
  });

  it('keeps Event projection on the originating Thread after navigation', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse());
    const { chatStore } = setupStores();
    chatStore.userInput = '原会话问题';
    const sending = chatStore.handleSend();
    await flushPromises();

    vi.mocked(getThreadMessages).mockResolvedValueOnce({
      messages: [threadMessage(1, 'user', '另一会话', { id: 21 })],
      previous_cursor: null,
    });
    await chatStore.loadSession('thread-2');

    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 1, 'run.created', {
        user_message_id: 11,
        assistant_message_id: 12,
      })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 2, 'run.started'));
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 3, 'tool.progress', {
        tool_name: 'search_knowledge_base',
        step: { label: '检索中', group: 'retrieval' },
      })
    );
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 4, 'message.completed', {
        content: '原会话回答',
      })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 5, 'run.completed'));
    streams.finish('run_1', 5);
    await sending;

    expect(chatStore.sessionId).toBe('thread-2');
    expect(chatStore.messages.map((message) => message.text)).toEqual(['另一会话']);
    expect(chatStore.messagesBySession['thread-1'][1]).toMatchObject({
      text: '原会话回答',
      ragSteps: [{ label: '检索中', group: 'retrieval' }],
    });
  });

  it('resumes HITL on the same Run and same assistant message', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse());
    vi.mocked(resumeRun).mockResolvedValue({
      run: runRecord('run_1', 'thread-1', 'pending'),
      checkpoint_id: 'checkpoint_1',
      created: true,
    });
    const { chatStore } = setupStores();
    chatStore.userInput = '这个角色是什么属性？';
    const firstTurn = chatStore.handleSend();
    await flushPromises();

    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 1, 'run.created', {
        user_message_id: 11,
        assistant_message_id: 12,
      })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 2, 'run.started'));
    streams.emit('run_1', event('run_1', 'thread-1', 3, 'run.waiting_input'));
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 4, 'hitl.required', {
        hitl_token: 'hitl_1',
        checkpoint_id: 'checkpoint_1',
        prompt: '请补充角色名',
        options: ['丹瑾', '丹恒'],
        route: 'clarify',
        retrieval_status: 'needs_clarification',
      })
    );
    streams.finish('run_1', 4);
    await firstTurn;

    expect(chatStore.messages).toHaveLength(2);
    expect(chatStore.currentPendingHitl).toMatchObject({
      runId: 'run_1',
      hitlToken: 'hitl_1',
      prompt: '请补充角色名',
    });
    expect(chatStore.messages[1]).toMatchObject({
      id: 12,
      runId: 'run_1',
      isHitlRequest: true,
      hitlOptions: ['丹瑾', '丹恒'],
    });

    chatStore.userInput = '丹瑾';
    const resumed = chatStore.handleSend();
    await flushPromises();
    const resumedConnection = streams.connections.get('run_1');
    expect(resumeRun).toHaveBeenCalledWith(
      'run_1',
      expect.objectContaining({ hitl_token: 'hitl_1', answer: '丹瑾' }),
      'test-token'
    );

    resumedConnection?.options.onEvent(
      event('run_1', 'thread-1', 5, 'hitl.resumed', { answer: '丹瑾' })
    );
    resumedConnection?.options.onEvent(
      event('run_1', 'thread-1', 6, 'message.completed', {
        content: '丹瑾是湮灭属性。',
      })
    );
    resumedConnection?.options.onEvent(event('run_1', 'thread-1', 7, 'run.completed'));
    resumedConnection?.result.resolve(7);
    await resumed;

    expect(chatStore.messages).toHaveLength(2);
    expect(chatStore.messages[1]).toMatchObject({
      id: 12,
      runId: 'run_1',
      text: '丹瑾是湮灭属性。',
      hitlResumeText: '丹瑾',
      isHitlRequest: false,
    });
    expect(chatStore.currentPendingHitl).toBeNull();
  });

  it('requests cancel without aborting SSE and waits for the authoritative terminal', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse());
    vi.mocked(cancelRun).mockResolvedValue(runRecord('run_1', 'thread-1', 'cancelling'));
    const { chatStore } = setupStores();
    chatStore.userInput = '需要停止的问题';
    const sending = chatStore.handleSend();
    await flushPromises();

    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 1, 'run.created', {
        user_message_id: 11,
        assistant_message_id: 12,
      })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 2, 'run.started'));
    chatStore.handleStop();
    await flushPromises();

    expect(cancelRun).toHaveBeenCalledWith('run_1', 'test-token');
    expect(streams.connections.get('run_1')?.options.signal?.aborted).toBe(false);
    expect(chatStore.currentRunStatus).toBe('cancelling');

    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 3, 'message.completed', {
        content: '已保存部分回答',
        status: 'incomplete',
      })
    );
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 4, 'run.cancelled', {
        error: { code: 'RUN_CANCELLED', message: '运行已取消', retryable: false },
      })
    );
    streams.finish('run_1', 4);
    await sending;

    expect(chatStore.messages[1]).toMatchObject({
      text: '已保存部分回答',
      status: 'incomplete',
      isThinking: false,
    });
    expect(chatStore.currentRunStatus).toBeNull();
  });

  it('restores a waiting Run by replaying durable events after page load', async () => {
    vi.mocked(getThreadMessages).mockResolvedValueOnce({
      messages: [
        threadMessage(1, 'user', '角色属性？', { id: 11, run_id: 'run_1' }),
        threadMessage(2, 'assistant', '', {
          id: 12,
          run_id: 'run_1',
          status: 'waiting_input',
        }),
      ],
      previous_cursor: null,
    });
    vi.mocked(getRun).mockResolvedValue(runRecord('run_1', 'thread-1', 'waiting_input'));
    vi.mocked(getRunEvents).mockResolvedValue({
      events: [
        event('run_1', 'thread-1', 1, 'run.created', {
          user_message_id: 11,
          assistant_message_id: 12,
        }),
        event('run_1', 'thread-1', 2, 'run.started'),
        event('run_1', 'thread-1', 3, 'run.waiting_input'),
        event('run_1', 'thread-1', 4, 'hitl.required', {
          hitl_token: 'hitl_1',
          checkpoint_id: 'checkpoint_1',
          prompt: '请补充角色名',
        }),
      ] as any,
      next_after: 4,
    });
    const { chatStore } = setupStores();

    await chatStore.loadSession('thread-1');

    expect(getRun).toHaveBeenCalledWith('run_1', 'test-token');
    expect(getRunEvents).toHaveBeenCalledWith(
      'run_1',
      'test-token',
      expect.objectContaining({ after: 0 })
    );
    expect(connectRunEventStream).not.toHaveBeenCalled();
    expect(chatStore.currentPendingHitl).toMatchObject({
      runId: 'run_1',
      hitlToken: 'hitl_1',
      prompt: '请补充角色名',
    });
    expect(chatStore.messages[1].isHitlRequest).toBe(true);
  });

  it('allows different Threads to run concurrently without cross-writing', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun)
      .mockResolvedValueOnce(createResponse('run_1', 'thread-1'))
      .mockResolvedValueOnce(createResponse('run_2', 'thread-2'));
    const { chatStore } = setupStores();
    chatStore.userInput = '线程一';
    const first = chatStore.handleSend();
    await flushPromises();

    chatStore.setViewedSession('thread-2', []);
    expect(chatStore.isLoading).toBe(false);
    chatStore.userInput = '线程二';
    const second = chatStore.handleSend();
    await flushPromises();

    expect(createRun).toHaveBeenCalledTimes(2);
    expect(chatStore.messagesBySession['thread-1'][0].text).toBe('线程一');
    expect(chatStore.messagesBySession['thread-2'][0].text).toBe('线程二');

    for (const [runId, threadId, answer] of [
      ['run_1', 'thread-1', '回答一'],
      ['run_2', 'thread-2', '回答二'],
    ] as const) {
      streams.emit(
        runId,
        event(runId, threadId, 1, 'run.created', {
          user_message_id: runId === 'run_1' ? 11 : 21,
          assistant_message_id: runId === 'run_1' ? 12 : 22,
        })
      );
      streams.emit(runId, event(runId, threadId, 2, 'run.started'));
      streams.emit(runId, event(runId, threadId, 3, 'message.completed', { content: answer }));
      streams.emit(runId, event(runId, threadId, 4, 'run.completed'));
      streams.finish(runId, 4);
    }
    await Promise.all([first, second]);

    expect(chatStore.messagesBySession['thread-1'][1].text).toBe('回答一');
    expect(chatStore.messagesBySession['thread-2'][1].text).toBe('回答二');
  });

  it('loads the latest message page and prepends older messages on demand', async () => {
    vi.mocked(getThreadMessages)
      .mockResolvedValueOnce({
        messages: [threadMessage(3, 'user', '最近问题'), threadMessage(4, 'assistant', '最近回答')],
        previous_cursor: 3,
      })
      .mockResolvedValueOnce({
        messages: [threadMessage(1, 'user', '更早问题'), threadMessage(2, 'assistant', '更早回答')],
        previous_cursor: null,
      });
    const { chatStore } = setupStores();

    await chatStore.loadSession('thread-1');

    expect(getThreadMessages).toHaveBeenCalledTimes(1);
    expect(getThreadMessages).toHaveBeenCalledWith('thread-1', { limit: 200 });
    expect(chatStore.messages.map((message) => message.text)).toEqual(['最近问题', '最近回答']);
    expect(chatStore.hasOlderMessages).toBe(true);

    await chatStore.loadOlderMessages();

    expect(getThreadMessages).toHaveBeenNthCalledWith(2, 'thread-1', {
      before: 3,
      limit: 200,
    });
    expect(chatStore.messages.map((message) => message.text)).toEqual([
      '更早问题',
      '更早回答',
      '最近问题',
      '最近回答',
    ]);
    expect(chatStore.hasOlderMessages).toBe(false);
  });

  it('renders a safe create failure without exposing transport details', async () => {
    vi.mocked(createRun).mockRejectedValue(new TypeError('secret socket detail'));
    const { chatStore } = setupStores();
    chatStore.userInput = '触发故障';

    await chatStore.handleSend();

    expect(chatStore.messages[1].text).toContain('[NETWORK_UNAVAILABLE]');
    expect(chatStore.messages[1].text).not.toContain('secret socket detail');
    expect(chatStore.messages[1]).toMatchObject({ status: 'failed', isThinking: false });
  });
});
