import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuthStore } from './auth';
import { useChatStore } from './chat';
import { useSessionStore } from './sessions';
import api from '@/utils/api';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: {
      get: vi.fn(),
      delete: vi.fn(),
    },
  };
});

type PendingRead = {
  resolve: (value: ReadableStreamReadResult<Uint8Array>) => void;
  reject: (reason?: unknown) => void;
};

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0));

const createLocalStorageMock = () => {
  const store = new Map<string, string>();
  return {
    getItem: vi.fn((key: string) => store.get(key) || null),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, value);
    }),
    removeItem: vi.fn((key: string) => {
      store.delete(key);
    }),
    clear: vi.fn(() => {
      store.clear();
    }),
  };
};

const createAbortError = () => {
  if (typeof DOMException !== 'undefined') {
    return new DOMException('The operation was aborted.', 'AbortError');
  }
  const error = new Error('The operation was aborted.');
  error.name = 'AbortError';
  return error;
};

const createControlledSseFetch = () => {
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const pendingReads: PendingRead[] = [];
  let closed = false;
  let failure: unknown;

  const reader = {
    read: vi.fn(() => {
      if (failure) {
        const reason = failure;
        failure = undefined;
        return Promise.reject(reason);
      }
      if (chunks.length) {
        return Promise.resolve({ done: false, value: chunks.shift() });
      }
      if (closed) {
        return Promise.resolve({ done: true, value: undefined });
      }
      return new Promise<ReadableStreamReadResult<Uint8Array>>((resolve, reject) => {
        pendingReads.push({ resolve, reject });
      });
    }),
  };

  const resolveNextRead = (value: ReadableStreamReadResult<Uint8Array>) => {
    const pending = pendingReads.shift();
    if (pending) {
      pending.resolve(value);
    } else if (!value.done && value.value) {
      chunks.push(value.value);
    }
  };

  const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
    init?.signal?.addEventListener('abort', () => {
      closed = true;
      const abortError = createAbortError();
      pendingReads.splice(0).forEach((pending) => pending.reject(abortError));
    });

    return Promise.resolve({
      ok: true,
      status: 200,
      body: {
        getReader: () => reader,
      },
    } as unknown as Response);
  });

  return {
    fetchMock,
    pushEvent(event: object) {
      resolveNextRead({
        done: false,
        value: encoder.encode(`data: ${JSON.stringify(event)}\n\n`),
      });
    },
    pushDone() {
      resolveNextRead({
        done: false,
        value: encoder.encode('data: [DONE]\n\n'),
      });
    },
    close() {
      closed = true;
      resolveNextRead({ done: true, value: undefined });
    },
    fail(reason: unknown) {
      const pending = pendingReads.shift();
      if (pending) {
        pending.reject(reason);
      } else {
        failure = reason;
      }
    },
  };
};

const setupStores = () => {
  setActivePinia(createPinia());

  const authStore = useAuthStore();
  authStore.token = 'test-token';
  authStore.currentUser = { username: 'tester', role: 'user' };

  const chatStore = useChatStore();
  chatStore.setViewedSession('session_current', []);

  return {
    authStore,
    chatStore,
    sessionStore: useSessionStore(),
  };
};

describe('chat store streaming sessions', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal('localStorage', createLocalStorageMock());
    vi.stubGlobal('alert', vi.fn());
    vi.stubGlobal('confirm', vi.fn(() => true));
  });

  it('clears account-scoped chat state when the authenticated workspace changes', () => {
    const { chatStore } = setupStores();
    const previousSessionId = chatStore.sessionId;

    chatStore.messagesBySession.session_current = [
      { text: '上一个账号的消息', isUser: true },
    ];
    chatStore.messages = chatStore.messagesBySession.session_current;
    chatStore.userInput = '未发送的草稿';
    chatStore.activeNav = 'settings';
    chatStore.pendingHitlBySession.session_current = {
      prompt: '请补充信息',
      options: [],
    };

    chatStore.resetWorkspace();

    expect(chatStore.messages).toEqual([]);
    expect(chatStore.messagesBySession).toEqual({});
    expect(chatStore.userInput).toBe('');
    expect(chatStore.activeNav).toBe('newChat');
    expect(chatStore.pendingHitlBySession).toEqual({});
    expect(chatStore.sessionId).not.toBe(previousSessionId);
  });

  it('creates a local history session with the user message and thinking placeholder immediately', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore, sessionStore } = setupStores();

    chatStore.userInput = '帮我总结一下文档';
    const sendPromise = chatStore.handleSend();
    await flushPromises();

    expect(sessionStore.sessions[0]).toMatchObject({
      session_id: 'session_current',
      isStreaming: true,
    });
    expect(chatStore.messagesBySession.session_current).toHaveLength(2);
    expect(chatStore.messagesBySession.session_current[0]).toMatchObject({
      text: '帮我总结一下文档',
      isUser: true,
    });
    expect(chatStore.messagesBySession.session_current[1]).toMatchObject({
      text: '',
      isUser: false,
      isThinking: true,
    });

    stream.pushDone();
    await sendPromise;
  });

  it('renders structured stream errors without exposing raw provider details', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore } = setupStores();

    chatStore.userInput = '触发故障';
    const sendPromise = chatStore.handleSend();
    await flushPromises();

    stream.pushEvent({ type: 'content', content: '已经生成的安全片段' });
    stream.pushEvent({
      type: 'error',
      error: {
        code: 'MODEL_RATE_LIMITED',
        message: '上游模型服务当前繁忙，请稍后重试',
        retryable: true,
      },
      content: 'raw-secret-upstream-body',
    });
    stream.close();
    await sendPromise;

    const message = chatStore.messagesBySession.session_current[1];
    expect(message.text).toContain('已经生成的安全片段');
    expect(message.text).toContain('MODEL_RATE_LIMITED');
    expect(message.text).toContain('上游模型服务当前繁忙');
    expect(message.text).not.toContain('raw-secret-upstream-body');
  });

  it('normalizes non-2xx fetch responses through the public error contract', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        headers: new Headers({ 'Retry-After': '2' }),
        json: async () => ({
          error: {
            code: 'MODEL_RATE_LIMITED',
            message: 'raw provider response',
            retryable: true,
            category: 'provider',
          },
        }),
      })
    );
    const { chatStore } = setupStores();

    chatStore.userInput = '触发限流';
    await chatStore.handleSend();

    const message = chatStore.messagesBySession.session_current[1];
    expect(message.text).toContain('[MODEL_RATE_LIMITED]');
    expect(message.text).toContain('上游模型服务当前繁忙');
    expect(message.text).not.toContain('raw provider response');
  });

  it('keeps partial text when the stream transport fails', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore } = setupStores();

    chatStore.userInput = '传输故障';
    const sendPromise = chatStore.handleSend();
    await flushPromises();
    stream.pushEvent({ type: 'content', content: '已经收到的回答' });
    await flushPromises();
    stream.fail(new TypeError('secret socket detail'));
    await sendPromise;

    const message = chatStore.messagesBySession.session_current[1];
    expect(message.text).toContain('已经收到的回答');
    expect(message.text).toContain('[NETWORK_UNAVAILABLE]');
    expect(message.text).not.toContain('secret socket detail');
  });

  it('treats an empty premature EOF as a safe transport failure', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore } = setupStores();

    chatStore.userInput = '空响应';
    const sendPromise = chatStore.handleSend();
    await flushPromises();
    stream.close();
    await sendPromise;

    const message = chatStore.messagesBySession.session_current[1];
    expect(message.isThinking).toBe(false);
    expect(message.text).toContain('[NETWORK_UNAVAILABLE]');
  });

  it('keeps partial text when the stream closes before DONE', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore } = setupStores();

    chatStore.userInput = '提前结束';
    const sendPromise = chatStore.handleSend();
    await flushPromises();
    stream.pushEvent({ type: 'content', content: '已收到部分回答' });
    await flushPromises();
    stream.close();
    await sendPromise;

    const message = chatStore.messagesBySession.session_current[1];
    expect(message.text).toContain('已收到部分回答');
    expect(message.text).toContain('[NETWORK_UNAVAILABLE]');
  });

  it('keeps streaming chunks on the originating session after viewing another history session', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    vi.mocked(api.get).mockResolvedValue({
      data: {
        messages: [
          {
            type: 'human',
            content: '旧问题',
            timestamp: '2026-07-08T00:00:00',
          },
          {
            type: 'ai',
            content: '旧回答',
            timestamp: '2026-07-08T00:00:01',
          },
        ],
      },
    });

    const { chatStore } = setupStores();
    chatStore.userInput = '新的问题';
    const sendPromise = chatStore.handleSend();
    await flushPromises();

    await chatStore.loadSession('session_old');
    expect(chatStore.sessionId).toBe('session_old');
    expect(chatStore.messages.map((msg) => msg.text)).toEqual(['旧问题', '旧回答']);

    stream.pushEvent({ type: 'rag_step', step: { label: '检索中', group: null } });
    await flushPromises();

    stream.pushEvent({ type: 'content', content: '正在回答' });
    await flushPromises();

    expect(chatStore.messagesBySession.session_current[1]).toMatchObject({
      text: '正在回答',
      isThinking: false,
    });
    expect(chatStore.messagesBySession.session_current[1].ragSteps?.[0]).toMatchObject({
      label: '检索中',
    });
    expect(chatStore.messages.map((msg) => msg.text)).toEqual(['旧问题', '旧回答']);

    vi.mocked(api.get).mockClear();
    await chatStore.loadSession('session_current');

    expect(api.get).not.toHaveBeenCalled();
    expect(chatStore.sessionId).toBe('session_current');
    expect(chatStore.messages[1]).toMatchObject({
      text: '正在回答',
      isThinking: false,
    });

    stream.pushDone();
    await sendPromise;
  });

  it('writes abort state only to the streaming session', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    vi.mocked(api.get).mockResolvedValue({
      data: {
        messages: [
          {
            type: 'human',
            content: '另一个会话',
            timestamp: '2026-07-08T00:00:00',
          },
        ],
      },
    });

    const { chatStore } = setupStores();
    chatStore.userInput = '要被终止的问题';
    const sendPromise = chatStore.handleSend();
    await flushPromises();

    await chatStore.loadSession('session_other');
    chatStore.handleStop();
    await sendPromise;

    expect(chatStore.messagesBySession.session_current[1]).toMatchObject({
      text: '(已终止回答)',
      isThinking: false,
    });
    expect(chatStore.messagesBySession.session_other.map((msg) => msg.text)).toEqual([
      '另一个会话',
    ]);
    expect(chatStore.sessionId).toBe('session_other');
    expect(chatStore.isLoading).toBe(false);
    expect(chatStore.streamingSessionId).toBeNull();
  });

  it('turns hitl_request events into a pending HITL prompt', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore } = setupStores();

    chatStore.userInput = '这个角色的属性是什么？';
    const sendPromise = chatStore.handleSend();
    await flushPromises();

    stream.pushEvent({
      type: 'trace',
      rag_trace: {
        retrieval_status: 'needs_clarification',
        route: 'clarify',
        hitl_prompt: '请补充角色名',
        hitl_options: ['丹瑾', '丹恒'],
      },
    });
    await flushPromises();

    stream.pushEvent({
      type: 'hitl_request',
      hitl: {
        id: 'hitl-1',
        prompt: '请补充角色名',
        options: ['丹瑾', '丹恒'],
        route: 'clarify',
        retrieval_status: 'needs_clarification',
        original_question: '这个角色的属性是什么？',
      },
    });
    stream.pushDone();
    await sendPromise;

    expect(chatStore.messagesBySession.session_current[1]).toMatchObject({
      isThinking: false,
      isHitlRequest: true,
      hitlPrompt: '请补充角色名',
      hitlOptions: ['丹瑾', '丹恒'],
    });
    expect(chatStore.pendingHitlBySession.session_current).toMatchObject({
      prompt: '请补充角色名',
      options: ['丹瑾', '丹恒'],
    });
    expect(chatStore.inputPlaceholder).toBe('输入自定义补充，或选择上方选项后发送...');
  });

  it('marks the next user message as a HITL answer and clears pending state after content streams', async () => {
    const stream = createControlledSseFetch();
    vi.stubGlobal('fetch', stream.fetchMock);
    const { chatStore } = setupStores();
    chatStore.pendingHitlBySession.session_current = {
      id: 'hitl-1',
      prompt: '请补充角色名',
      options: ['丹瑾'],
    };

    chatStore.userInput = '丹瑾';
    const sendPromise = chatStore.handleSend();
    await flushPromises();

    expect(chatStore.messagesBySession.session_current[0]).toMatchObject({
      text: '丹瑾',
      isUser: true,
      isHitlAnswer: true,
    });
    expect(chatStore.messagesBySession.session_current[1]).toMatchObject({
      isUser: false,
      hitlResumeText: '丹瑾',
    });
    expect(chatStore.pendingHitlBySession.session_current).toBeUndefined();

    stream.pushEvent({ type: 'content', content: '丹瑾是湮灭属性。' });
    stream.pushDone();
    await sendPromise;

    expect(chatStore.messagesBySession.session_current[1]).toMatchObject({
      text: '丹瑾是湮灭属性。',
      isThinking: false,
      hitlResumeText: '丹瑾',
    });
    expect(chatStore.pendingHitlBySession.session_current).toBeUndefined();
  });

  it('maps persisted HITL answer turns as continuation state instead of normal chat turns', () => {
    const { chatStore } = setupStores();

    const messages = chatStore.mapServerMessages([
      { type: 'human', content: '这个角色的属性是什么？' },
      {
        type: 'ai',
        content: '请补充角色名',
        rag_trace: {
          retrieval_status: 'needs_clarification',
          route: 'clarify',
          hitl_prompt: '请补充角色名',
        },
      },
      { type: 'human', content: '丹瑾' },
      { type: 'ai', content: '丹瑾是湮灭属性。' },
    ]);

    expect(messages[1]).toMatchObject({ isHitlRequest: true });
    expect(messages[2]).toMatchObject({ isHitlAnswer: true });
    expect(messages[3]).toMatchObject({
      text: '丹瑾是湮灭属性。',
      hitlResumeText: '丹瑾',
    });
  });
});
