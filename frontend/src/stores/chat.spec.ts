import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clearAuthSession, installAuthSession } from '@/auth/session';
import { connectRunEventStream } from '@/events/runEventStream';
import { cancelRun, createRun, getRun, getRunEvents, resumeRun } from '@/runs/runClient';
import { createThread, deleteThread, getThreadMessages } from '@/threads/threadClient';
import type { RunEventType, RunEventV1 } from '@/types/chat';
import { useAuthStore } from './auth';
import { useChatStore } from './chat';
import { useRunsStore } from './runs';
import { useThreadStore } from './threads';
import { useCapabilityStore } from './capabilities';

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
  type: RunEventType,
  data: Record<string, unknown> = {}
): RunEventV1 {
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
  const threadStore = useThreadStore();
  if (threadId) threadStore.threads = [threadListItem(threadId)];
  chatStore.setViewedThread(threadId, []);
  return {
    authStore,
    chatStore,
    runsStore: useRunsStore(),
    threadStore,
    capabilityStore: useCapabilityStore(),
  };
}

function installCapabilityCatalog() {
  const store = useCapabilityStore();
  store.catalog = {
    schema_version: 1,
    catalog_hash: 'a'.repeat(64),
    skills: [
      {
        name: 'web-research',
        version: '1.0.0',
        description: 'Research public web evidence.',
        activation: '/web-research',
        available: true,
        availability_reason: null,
        required_roles: [],
        tool_names: ['web_search', 'web_fetch'],
        approval_tools: [],
        network_policies: ['restricted'],
        resource_scopes: ['public-web'],
      },
      {
        name: 'sandbox',
        version: '1.0.0',
        description: 'Execute isolated code.',
        activation: '/sandbox',
        available: true,
        availability_reason: null,
        required_roles: ['admin'],
        tool_names: ['sandbox_execute'],
        approval_tools: ['sandbox_execute'],
        network_policies: ['none'],
        resource_scopes: ['code-execution'],
      },
    ],
    tools: [],
  };
  return store;
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
    emit(runId: string, item: RunEventV1) {
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
    chatStore.messagesByThread['thread-1'] = [{ text: '旧账号消息', isUser: true }];
    chatStore.messages = chatStore.messagesByThread['thread-1'];
    chatStore.userInput = '草稿';
    runsStore.ensure('run_1', 'thread-1').status = 'running';

    chatStore.resetWorkspace();

    expect(chatStore.messagesByThread).toEqual({});
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
    expect(chatStore.threadId).toBe('thread-server');

    streams.emit('run_1', event('run_1', 'thread-server', 1, 'run.created'));
    streams.emit(
      'run_1',
      event('run_1', 'thread-server', 2, 'message.completed', { content: '第一条回答' })
    );
    streams.emit('run_1', event('run_1', 'thread-server', 3, 'run.completed'));
    streams.finish('run_1', 3);
    await sending;
  });

  it('returns from history to a local draft without creating an empty Thread', async () => {
    const { chatStore, threadStore } = setupStores('thread-1');
    chatStore.activeNav = 'history';
    threadStore.showHistorySidebar = true;

    await chatStore.handleNewChat();

    expect(createThread).not.toHaveBeenCalled();
    expect(threadStore.threads.map((thread) => thread.thread_id)).toEqual(['thread-1']);
    expect(chatStore.threadId).toBe('');
    expect(chatStore.messages).toEqual([]);
    expect(chatStore.activeNav).toBe('newChat');
    expect(threadStore.showHistorySidebar).toBe(false);
  });

  it('keeps the user-facing prompt clean while activating Web Research for the Run', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse('run_1', 'thread-1'));
    const { chatStore } = setupStores();
    const capabilityStore = installCapabilityCatalog();
    capabilityStore.selectSkill('web-research');
    chatStore.userInput = '核验今天的公开发布信息';

    const sending = chatStore.handleSend();
    await flushPromises();

    expect(createRun).toHaveBeenCalledWith(
      'thread-1',
      expect.objectContaining({
        message: '/web-research\n核验今天的公开发布信息',
        approved_tools: [],
      }),
      'test-token'
    );
    expect(chatStore.messages[0]).toMatchObject({
      text: '核验今天的公开发布信息',
      skillName: 'web-research',
    });

    streams.emit('run_1', event('run_1', 'thread-1', 1, 'run.created'));
    streams.emit(
      'run_1',
      event('run_1', 'thread-1', 2, 'message.completed', { content: '研究完成' })
    );
    streams.emit('run_1', event('run_1', 'thread-1', 3, 'run.completed'));
    streams.finish('run_1', 3);
    await sending;
  });

  it('opens a real pre-Run approval flow before sending Sandbox source', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse('run_2', 'thread-1'));
    const { chatStore } = setupStores();
    const capabilityStore = installCapabilityCatalog();
    capabilityStore.selectSkill('sandbox');
    capabilityStore.setSandboxLanguage('python');
    chatStore.userInput = 'print(6 * 7)';

    await chatStore.handleSend();

    expect(capabilityStore.approvalOpen).toBe(true);
    expect(createRun).not.toHaveBeenCalled();

    capabilityStore.confirmPendingApproval();
    const sending = chatStore.handleSend({ approvalConfirmed: true });
    await flushPromises();

    expect(createRun).toHaveBeenCalledWith(
      'thread-1',
      expect.objectContaining({
        message: expect.stringContaining('"source": "print(6 * 7)"'),
        approved_tools: ['sandbox_execute'],
      }),
      'test-token'
    );
    expect(capabilityStore.pendingApprovalDraft?.confirmed).toBe(false);

    streams.emit('run_2', event('run_2', 'thread-1', 1, 'run.created'));
    streams.emit(
      'run_2',
      event('run_2', 'thread-1', 2, 'message.completed', { content: '输出 42' })
    );
    streams.emit('run_2', event('run_2', 'thread-1', 3, 'run.completed'));
    streams.finish('run_2', 3);
    await sending;
  });

  it('restores an approved Sandbox draft when Run creation fails before reservation', async () => {
    vi.mocked(createRun).mockRejectedValue({
      code: 'SERVICE_UNAVAILABLE',
      message: '运行服务暂不可用',
      retryable: true,
      category: 'run',
    });
    const { chatStore } = setupStores();
    const capabilityStore = installCapabilityCatalog();
    capabilityStore.selectSkill('sandbox');
    chatStore.userInput = 'print("retry")';
    capabilityStore.openApproval();
    capabilityStore.confirmPendingApproval();

    await expect(chatStore.handleSend({ approvalConfirmed: true })).rejects.toMatchObject({
      code: 'SERVICE_UNAVAILABLE',
    });

    expect(chatStore.userInput).toBe('print("retry")');
    expect(chatStore.messages).toEqual([]);
    expect(capabilityStore.pendingApprovalDraft?.confirmed).toBe(false);
  });

  it('clears approved Sandbox state when creating the first Thread fails', async () => {
    vi.mocked(createThread).mockRejectedValue({
      code: 'SERVICE_UNAVAILABLE',
      message: 'Thread 服务暂不可用',
      retryable: true,
      category: 'thread',
    });
    const { chatStore } = setupStores('');
    const capabilityStore = installCapabilityCatalog();
    capabilityStore.selectSkill('sandbox');
    chatStore.userInput = 'print("new thread")';
    capabilityStore.openApproval();
    capabilityStore.confirmPendingApproval();

    await chatStore.handleSend({ approvalConfirmed: true });

    expect(createRun).not.toHaveBeenCalled();
    expect(chatStore.userInput).toBe('print("new thread")');
    expect(capabilityStore.pendingApprovalDraft?.confirmed).toBe(false);
    expect(alert).toHaveBeenCalledWith('Thread 服务暂不可用');
  });

  it('authoritatively deletes the current Thread before returning to a local draft', async () => {
    vi.mocked(deleteThread).mockResolvedValue({
      thread_id: 'thread-1',
      message: '成功删除 Thread',
    });
    const { chatStore, threadStore } = setupStores();
    chatStore.messagesByThread['thread-1'] = [{ text: '旧消息', isUser: true }];
    chatStore.messages = chatStore.messagesByThread['thread-1'];

    await chatStore.handleClearChat();

    expect(deleteThread).toHaveBeenCalledWith('thread-1');
    expect(createThread).not.toHaveBeenCalled();
    expect(chatStore.threadId).toBe('');
    expect(chatStore.messages).toEqual([]);
    expect(chatStore.messagesByThread['thread-1']).toBeUndefined();
    expect(threadStore.threads).toEqual([]);
  });

  it('keeps the current Thread intact when authoritative deletion reports an active Run', async () => {
    vi.mocked(deleteThread).mockRejectedValue({
      code: 'RUN_ACTIVE',
      message: 'Thread 仍有活跃 Run',
      retryable: false,
      category: 'thread',
    });
    const { chatStore, threadStore } = setupStores();
    chatStore.messagesByThread['thread-1'] = [{ text: '仍需保留', isUser: true }];
    chatStore.messages = chatStore.messagesByThread['thread-1'];

    await chatStore.handleClearChat();

    expect(createThread).not.toHaveBeenCalled();
    expect(chatStore.threadId).toBe('thread-1');
    expect(chatStore.messages[0].text).toBe('仍需保留');
    expect(threadStore.threads[0].thread_id).toBe('thread-1');
    expect(alert).toHaveBeenCalledWith('Thread 仍有活跃 Run');
  });

  it('locks input from canonical active Run metadata until the Run is restored', () => {
    const { chatStore, threadStore } = setupStores();
    threadStore.threads[0].active_run_id = 'run-server';
    threadStore.threads[0].active_run_status = 'running';
    threadStore.threads[0].activeRunId = 'run-server';
    threadStore.threads[0].activeRunStatus = 'running';
    threadStore.threads[0].isStreaming = true;

    expect(chatStore.currentRunStatus).toBe('running');
    expect(chatStore.isInputLocked).toBe(true);
    expect(chatStore.isViewingStreamingThread).toBe(false);
  });

  it('restores the persisted Skill when reopening a Thread', async () => {
    const { chatStore, capabilityStore } = setupStores();
    installCapabilityCatalog();
    vi.mocked(getThreadMessages).mockResolvedValue({
      messages: [
        threadMessage(1, 'user', '/web-research\n旧问题', {
          run_id: 'run-history',
          skill_name: 'web-research',
        }),
        threadMessage(2, 'assistant', '旧回答', {
          run_id: 'run-history',
          skill_name: 'web-research',
        }),
      ],
      previous_cursor: null,
    });

    await chatStore.loadThread('thread-1');

    expect(capabilityStore.selectedSkillName).toBe('web-research');
    expect(chatStore.messages[0].skillName).toBe('web-research');
    expect(chatStore.messages[0].text).toBe('旧问题');
  });

  it('treats the latest general Run as an explicit null Skill selection', async () => {
    const { chatStore, capabilityStore } = setupStores();
    installCapabilityCatalog();
    vi.mocked(getThreadMessages).mockResolvedValue({
      messages: [
        threadMessage(1, 'user', '/web-research\n旧问题', {
          run_id: 'run-web',
          skill_name: 'web-research',
        }),
        threadMessage(2, 'assistant', '旧回答', {
          run_id: 'run-web',
          skill_name: 'web-research',
        }),
        threadMessage(3, 'user', '最新通用问题', {
          run_id: 'run-general',
          skill_name: null,
        }),
        threadMessage(4, 'assistant', '最新通用回答', {
          run_id: 'run-general',
          skill_name: null,
        }),
      ],
      previous_cursor: null,
    });
    vi.mocked(getRun).mockResolvedValue(runRecord('run-general', 'thread-1', 'succeeded'));
    vi.mocked(getRunEvents).mockResolvedValue({
      events: [
        event('run-general', 'thread-1', 1, 'run.created', {
          status: 'pending',
          user_message_id: 3,
          assistant_message_id: 4,
        }),
        event('run-general', 'thread-1', 2, 'message.completed', {
          content: '最新通用回答',
          status: 'completed',
        }),
        event('run-general', 'thread-1', 3, 'run.completed'),
      ],
      next_after: 3,
    });

    await chatStore.loadThread('thread-1');

    expect(capabilityStore.selectedSkillName).toBeNull();
  });

  it.each(['completed', 'failed', 'cancelled'] as const)(
    'keeps a persisted %s Message authoritative when Run lookup transport fails',
    async (messageStatus) => {
      const runId = `run-${messageStatus}`;
      const content = `${messageStatus} 持久正文`;
      const { chatStore, runsStore } = setupStores();
      vi.mocked(getThreadMessages).mockResolvedValue({
        messages: [
          threadMessage(1, 'user', '问题', { run_id: runId }),
          threadMessage(2, 'assistant', content, {
            run_id: runId,
            status: messageStatus,
          }),
        ],
        previous_cursor: null,
      });
      vi.mocked(getRun).mockRejectedValue(new TypeError('offline lookup'));

      await chatStore.loadThread('thread-1');

      expect(chatStore.messages[1]).toMatchObject({
        status: messageStatus,
        isThinking: false,
        text: content,
      });
      expect(runsStore.byId[runId]).toBeUndefined();
    }
  );

  it.each([
    ['completed', 'succeeded'],
    ['failed', 'failed'],
    ['cancelled', 'cancelled'],
  ] as const)(
    'keeps a persisted %s Message authoritative when Event Journal transport fails',
    async (messageStatus, runStatus) => {
      const runId = `run-${messageStatus}`;
      const content = `${messageStatus} 持久正文`;
      const { chatStore, runsStore } = setupStores();
      vi.mocked(getThreadMessages).mockResolvedValue({
        messages: [
          threadMessage(1, 'user', '问题', { run_id: runId }),
          threadMessage(2, 'assistant', content, {
            run_id: runId,
            status: messageStatus,
          }),
        ],
        previous_cursor: null,
      });
      vi.mocked(getRun).mockResolvedValue(runRecord(runId, 'thread-1', runStatus));
      vi.mocked(getRunEvents).mockRejectedValue(new TypeError('offline journal'));

      await chatStore.loadThread('thread-1');

      expect(chatStore.messages[1]).toMatchObject({
        status: messageStatus,
        isThinking: false,
        text: content,
      });
      expect(runsStore.byId[runId]?.transportError?.code).toBe('NETWORK_UNAVAILABLE');
    }
  );

  it('replays terminal Run Events so timeline and Artifacts survive a reload', async () => {
    const { chatStore } = setupStores();
    vi.mocked(getThreadMessages).mockResolvedValue({
      messages: [
        threadMessage(1, 'user', '生成报告', { run_id: 'run-terminal' }),
        threadMessage(2, 'assistant', '报告完成', {
          run_id: 'run-terminal',
          status: 'completed',
        }),
      ],
      previous_cursor: null,
    });
    vi.mocked(getRun).mockResolvedValue(runRecord('run-terminal', 'thread-1', 'succeeded'));
    vi.mocked(getRunEvents).mockResolvedValue({
      events: [
        event('run-terminal', 'thread-1', 1, 'run.created', { status: 'pending' }),
        event('run-terminal', 'thread-1', 2, 'run.started'),
        event('run-terminal', 'thread-1', 3, 'tool.started', {
          tool_name: 'sandbox_execute',
          tool_call_id: 'call-terminal',
        }),
        event('run-terminal', 'thread-1', 4, 'tool.completed', {
          tool_name: 'sandbox_execute',
          tool_call_id: 'call-terminal',
          duration_ms: 20,
          guardrail_decision: 'ALLOW',
          reason_code: 'ALLOWED',
        }),
        event('run-terminal', 'thread-1', 5, 'artifact.created', {
          artifact_id: 'art_terminal',
          name: 'report.json',
          media_type: 'application/json',
          uri: '/api/artifacts/art_terminal',
          tool_name: 'sandbox_execute',
          tool_call_id: 'call-terminal',
        }),
        event('run-terminal', 'thread-1', 6, 'message.completed', {
          content: '报告完成',
          status: 'completed',
        }),
        event('run-terminal', 'thread-1', 7, 'run.completed'),
      ] as any,
      next_after: 7,
    });

    await chatStore.loadThread('thread-1');

    const assistant = chatStore.messages.find((message) => !message.isUser);
    expect(assistant?.runTimeline).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: 'tool:call-terminal',
          status: 'completed',
          guardrailDecision: 'ALLOW',
        }),
      ])
    );
    expect(assistant?.artifacts).toEqual([
      expect.objectContaining({ artifactId: 'art_terminal', name: 'report.json' }),
    ]);
    expect(connectRunEventStream).not.toHaveBeenCalled();
  });

  it('creates optimistic messages, reserves a durable Run, and projects final authority', async () => {
    const streams = installControlledStreams();
    vi.mocked(createRun).mockResolvedValue(createResponse());
    const { chatStore, threadStore } = setupStores();
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
    expect(threadStore.threads[0]).toMatchObject({
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
    expect(threadStore.threads[0].isStreaming).toBe(false);
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
    await chatStore.loadThread('thread-2');

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

    expect(chatStore.threadId).toBe('thread-2');
    expect(chatStore.messages.map((message) => message.text)).toEqual(['另一会话']);
    expect(chatStore.messagesByThread['thread-1'][1]).toMatchObject({
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

    await chatStore.loadThread('thread-1');

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

    chatStore.setViewedThread('thread-2', []);
    expect(chatStore.isLoading).toBe(false);
    chatStore.userInput = '线程二';
    const second = chatStore.handleSend();
    await flushPromises();

    expect(createRun).toHaveBeenCalledTimes(2);
    expect(chatStore.messagesByThread['thread-1'][0].text).toBe('线程一');
    expect(chatStore.messagesByThread['thread-2'][0].text).toBe('线程二');

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

    expect(chatStore.messagesByThread['thread-1'][1].text).toBe('回答一');
    expect(chatStore.messagesByThread['thread-2'][1].text).toBe('回答二');
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

    await chatStore.loadThread('thread-1');

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

  it('does not reconstruct HITL state from Messages without a Run identity', async () => {
    vi.mocked(getThreadMessages).mockResolvedValueOnce({
      messages: [
        threadMessage(1, 'assistant', '请补充角色名', {
          rag_trace: {
            route: 'clarify',
            retrieval_status: 'needs_clarification',
            hitl_prompt: '请补充角色名',
          },
        }),
      ],
      previous_cursor: null,
    });
    const { chatStore } = setupStores();

    await chatStore.loadThread('thread-1');

    expect(chatStore.currentPendingHitl).toBeNull();
    expect(chatStore.messages[0]).toMatchObject({
      isHitlRequest: false,
      isHitlAnswer: false,
    });
  });

  it('keeps Message lifecycle authoritative when only Run transport fails', async () => {
    vi.mocked(createRun).mockResolvedValue(createResponse());
    vi.mocked(connectRunEventStream).mockRejectedValue(
      Object.assign(new Error('offline'), {
        code: 'NETWORK_UNAVAILABLE',
        retryable: true,
      })
    );
    const { chatStore, runsStore } = setupStores();
    chatStore.userInput = '继续执行';

    await chatStore.handleSend();

    expect(runsStore.byId.run_1).toMatchObject({
      status: 'pending',
      terminal: false,
      transportStatus: 'closed',
    });
    expect(runsStore.byId.run_1.transportError?.code).toBe('NETWORK_UNAVAILABLE');
    expect(chatStore.messages[1].status).not.toBe('failed');
    expect(chatStore.messages[1].text).not.toContain('[NETWORK_UNAVAILABLE]');
  });
});
