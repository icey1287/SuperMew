import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { connectRunEventStream } from '@/events/runEventStream';
import type { RuntimeRunEvent } from '@/events/runEventReducer';
import {
  cancelRun,
  createIdempotencyKey,
  createRun,
  getRun,
  getRunEvents,
  resumeRun,
} from '@/runs/runClient';
import { useRunsStore } from './runs';

vi.mock('@/runs/runClient', () => ({
  cancelRun: vi.fn(),
  createIdempotencyKey: vi.fn((scope: string) => `${scope}_generated_key`),
  createRun: vi.fn(),
  getRun: vi.fn(),
  getRunEvents: vi.fn(),
  resumeRun: vi.fn(),
}));

vi.mock('@/events/runEventStream', () => ({
  connectRunEventStream: vi.fn(),
}));

function event(
  sequence: number,
  type: RuntimeRunEvent['type'],
  data: Record<string, unknown> = {}
): RuntimeRunEvent {
  return {
    schema_version: 1,
    event_id: `evt_${sequence}`,
    sequence,
    run_id: 'run_1',
    thread_id: 'thread-1',
    type,
    timestamp: '2026-07-15T00:00:00Z',
    data,
  };
}

function runRecord(status = 'pending') {
  return {
    id: 'run_1',
    thread_id: 'thread-1',
    status,
    idempotency_key: 'run_generated_key',
    on_disconnect: 'continue',
  } as any;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('durable runs store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    vi.mocked(createIdempotencyKey).mockImplementation((scope) => `${scope}_generated_key`);
  });

  it('reuses the same create idempotency key after an uncertain transport failure', async () => {
    vi.mocked(createRun)
      .mockRejectedValueOnce(new TypeError('secret socket detail'))
      .mockResolvedValueOnce({
        run: runRecord(),
        created: false,
        thread_version: 2,
      });
    const store = useRunsStore();
    const command = {
      threadId: 'thread-1',
      message: 'hello',
      token: 'token',
    };

    await expect(store.create(command)).rejects.toMatchObject({
      code: 'NETWORK_UNAVAILABLE',
      retryable: true,
    });
    expect(store.pendingCreates['thread-1']).toEqual({
      message: 'hello',
      idempotencyKey: 'run_generated_key',
    });

    const created = await store.create(command);
    expect(created.idempotencyKey).toBe('run_generated_key');
    expect(store.pendingCreates['thread-1']).toBeUndefined();
    expect(store.byId.run_1.idempotencyKey).toBe('run_generated_key');
    expect(vi.mocked(createRun).mock.calls.map((call) => call[1])).toEqual([
      expect.objectContaining({ idempotency_key: 'run_generated_key' }),
      expect.objectContaining({ idempotency_key: 'run_generated_key' }),
    ]);
    expect(createRun).toHaveBeenLastCalledWith(
      'thread-1',
      expect.objectContaining({
        message: 'hello',
        idempotency_key: 'run_generated_key',
        multitask_strategy: 'reject',
        on_disconnect: 'continue',
        approved_tools: [],
      }),
      'token'
    );
  });

  it('rejects changing the payload while a create attempt is unresolved', async () => {
    vi.mocked(createRun).mockRejectedValue(new TypeError('offline'));
    const store = useRunsStore();
    await expect(
      store.create({ threadId: 'thread-1', message: 'first', token: 'token' })
    ).rejects.toMatchObject({ code: 'NETWORK_UNAVAILABLE' });

    await expect(
      store.create({ threadId: 'thread-1', message: 'different', token: 'token' })
    ).rejects.toMatchObject({ code: 'CONFLICT', retryable: false });
    expect(createRun).toHaveBeenCalledOnce();
  });

  it('uses the two-step create then GET stream flow and projects terminal state', async () => {
    vi.mocked(createRun).mockResolvedValue({
      run: runRecord(),
      created: true,
      thread_version: 2,
    });
    vi.mocked(connectRunEventStream).mockImplementation(async (options) => {
      options.onOpen?.(0);
      options.onEvent(
        event(1, 'run.created', {
          status: 'pending',
          user_message_id: 11,
          assistant_message_id: 12,
        })
      );
      options.onEvent(
        event(2, 'message.completed', {
          content: 'answer',
          rag_trace: { retrieval_outcome: 'ANSWERABLE' },
        })
      );
      options.onEvent(event(3, 'run.completed'));
      return 3;
    });
    const store = useRunsStore();

    await store.start({
      threadId: 'thread-1',
      message: 'hello',
      token: 'token',
    });

    expect(vi.mocked(createRun).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(connectRunEventStream).mock.invocationCallOrder[0]
    );
    expect(connectRunEventStream).toHaveBeenCalledWith(
      expect.objectContaining({
        runId: 'run_1',
        threadId: 'thread-1',
        token: 'token',
        after: 0,
      })
    );
    expect(store.byId.run_1).toMatchObject({
      status: 'completed',
      terminal: true,
      terminalSequence: 3,
      transportStatus: 'closed',
      userMessageId: 11,
      assistantMessageId: 12,
      messageText: 'answer',
      ragTrace: { retrieval_outcome: 'ANSWERABLE' },
    });
  });

  it('keeps transport reconnect state separate from the Run lifecycle', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    const observed: Array<[string, string, number]> = [];
    vi.mocked(connectRunEventStream).mockImplementation(async (options) => {
      options.onReconnect?.(
        2,
        0,
        Object.assign(new Error('offline'), {
          code: 'NETWORK_UNAVAILABLE',
          retryable: true,
        }) as any
      );
      observed.push([
        store.byId.run_1.status,
        store.byId.run_1.transportStatus,
        store.byId.run_1.reconnectAttempt,
      ]);
      options.onOpen?.(0);
      observed.push([
        store.byId.run_1.status,
        store.byId.run_1.transportStatus,
        store.byId.run_1.reconnectAttempt,
      ]);
      options.onEvent(
        event(1, 'hitl.required', {
          hitl_token: 'hitl_1',
          prompt: '请补充角色名',
        })
      );
      return 1;
    });

    await store.connect('run_1', 'token');

    expect(observed).toEqual([
      ['running', 'reconnecting', 2],
      ['running', 'open', 0],
    ]);
    expect(store.byId.run_1.status).toBe('waiting_input');
    expect(store.byId.run_1.transportStatus).toBe('closed');
    expect(store.byId.run_1.pendingHitl?.hitlToken).toBe('hitl_1');
  });

  it('reuses resume idempotency and reconnects the same Run after acceptance', async () => {
    const store = useRunsStore();
    store.apply(event(1, 'run.waiting_input'));
    store.apply(
      event(2, 'hitl.required', {
        hitl_token: 'hitl_1',
        checkpoint_id: 'checkpoint_1',
        prompt: '请补充角色名',
      })
    );
    vi.mocked(resumeRun)
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce({
        run: runRecord('pending'),
        checkpoint_id: 'checkpoint_1',
        created: false,
      });
    vi.mocked(connectRunEventStream).mockImplementation(async (options) => {
      options.onEvent(event(3, 'hitl.resumed', { answer: '丹瑾' }));
      options.onEvent(event(4, 'run.completed'));
      return 4;
    });
    const command = {
      token: 'token',
      hitlToken: 'hitl_1',
      answer: '丹瑾',
    };

    await expect(store.resume('run_1', command)).rejects.toMatchObject({
      code: 'NETWORK_UNAVAILABLE',
    });
    await store.resume('run_1', command);

    expect(vi.mocked(resumeRun).mock.calls.map((call) => call[1])).toEqual([
      {
        hitl_token: 'hitl_1',
        answer: '丹瑾',
        idempotency_key: 'resume_generated_key',
      },
      {
        hitl_token: 'hitl_1',
        answer: '丹瑾',
        idempotency_key: 'resume_generated_key',
      },
    ]);
    expect(connectRunEventStream).toHaveBeenCalledWith(
      expect.objectContaining({ runId: 'run_1', after: 2 })
    );
    expect(store.byId.run_1.lastResumeAnswer).toBe('丹瑾');
    expect(store.byId.run_1.status).toBe('completed');
    expect(store.pendingResumes.run_1).toBeUndefined();
  });

  it('does not abort the event stream when cancel is requested', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    const streamDone = deferred<number>();
    let streamOptions!: Parameters<typeof connectRunEventStream>[0];
    vi.mocked(connectRunEventStream).mockImplementation((options) => {
      streamOptions = options;
      options.onOpen?.(0);
      return streamDone.promise;
    });
    vi.mocked(cancelRun).mockResolvedValue(runRecord('cancelling'));

    const connected = store.connect('run_1', 'token');
    await Promise.resolve();
    await store.cancel('run_1', 'token');

    expect(streamOptions.signal?.aborted).toBe(false);
    expect(store.byId.run_1.status).toBe('cancelling');
    streamOptions.onEvent(event(1, 'message.completed', { content: 'partial' }));
    streamOptions.onEvent(event(2, 'run.cancelled'));
    streamDone.resolve(2);
    await connected;
    expect(store.byId.run_1.messageText).toBe('partial');
    expect(store.byId.run_1.status).toBe('cancelled');
  });

  it('still accepts authoritative final events after a terminal cancel response', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'waiting_input';
    vi.mocked(cancelRun).mockResolvedValue(runRecord('cancelled'));

    await store.cancel('run_1', 'token');
    expect(store.byId.run_1.terminal).toBe(true);
    expect(store.byId.run_1.terminalSequence).toBeNull();

    store.apply(event(1, 'message.completed', { content: '运行已由用户取消。' }));
    store.apply(event(2, 'run.cancelled'));
    expect(store.byId.run_1.messageText).toBe('运行已由用户取消。');
    expect(store.byId.run_1.terminalSequence).toBe(2);
  });

  it('does not let a stale cancel response overwrite an SSE terminal state', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    const cancelResponse = deferred<any>();
    vi.mocked(cancelRun).mockReturnValue(cancelResponse.promise);

    const cancelling = store.cancel('run_1', 'token');
    store.apply(event(1, 'run.completed'));
    cancelResponse.resolve(runRecord('cancelling'));
    await cancelling;

    expect(store.byId.run_1.status).toBe('completed');
    expect(store.byId.run_1.terminalSequence).toBe(1);
    expect(store.byId.run_1.error).toBeNull();
  });

  it('replays durable events and can explicitly disconnect a local stream', async () => {
    const store = useRunsStore();
    vi.mocked(getRun).mockResolvedValue(runRecord('running'));
    vi.mocked(getRunEvents).mockResolvedValue({
      events: [event(1, 'run.created', { status: 'pending' }), event(2, 'run.started')] as any,
      next_after: 2,
    });

    const replayed = await store.replay('run_1', 'token');
    expect(replayed.status).toBe('running');
    expect(replayed.lastSequence).toBe(2);

    let signal!: AbortSignal;
    vi.mocked(connectRunEventStream).mockImplementation((options) => {
      signal = options.signal as AbortSignal;
      return new Promise((resolve) =>
        signal.addEventListener('abort', () => resolve(options.after || 0), {
          once: true,
        })
      );
    });
    const connected = store.connect('run_1', 'token');
    await Promise.resolve();
    store.disconnect('run_1');
    await connected;

    expect(signal.aborted).toBe(true);
    expect(store.byId.run_1.transportStatus).toBe('closed');
  });
});
