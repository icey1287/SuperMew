import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import { useRunsStore } from './runs';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: {
      post: vi.fn(),
    },
  };
});

describe('runs store cancellation', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('marks a run cancelling before invoking the backend cancel endpoint', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    vi.mocked(api.post).mockImplementation(async () => {
      expect(store.byId.run_1.status).toBe('cancelling');
      return { data: { status: 'cancelled' } } as any;
    });

    await store.cancel('run_1');

    expect(api.post).toHaveBeenCalledWith('/v1/runs/run_1/cancel');
    expect(store.byId.run_1.status).toBe('cancelled');
    expect(store.byId.run_1.terminal).toBe(true);
  });

  it('exposes a retryable failure through getters', () => {
    const store = useRunsStore();
    store.apply({
      schema_version: 1,
      event_id: 'evt_1',
      sequence: 1,
      run_id: 'run_1',
      thread_id: 'thread-1',
      type: 'run.failed',
      timestamp: '2026-07-14T00:00:00Z',
      data: {
        error: {
          code: 'MODEL_RATE_LIMITED',
          message: '模型服务繁忙',
          retryable: true,
        },
      },
    });

    expect(store.failureForRun('run_1')?.code).toBe('MODEL_RATE_LIMITED');
    expect(store.canRetry('run_1')).toBe(true);
  });

  it('rolls cancelling state back and throws a redacted public error on failure', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    vi.mocked(api.post).mockRejectedValue({
      response: {
        status: 503,
        data: {
          secret: 'raw upstream body',
        },
      },
    });

    await expect(store.cancel('run_1')).rejects.toMatchObject({
      code: 'INTERNAL_ERROR',
      retryable: true,
      message: '服务暂时不可用，请稍后重试',
    });
    expect(store.byId.run_1.status).toBe('running');
    expect(store.byId.run_1.terminal).toBe(false);
  });

  it('hydrates the complete cancellation error returned by the backend', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    vi.mocked(api.post).mockResolvedValue({
      data: {
        id: 'run_1',
        thread_id: 'thread-1',
        status: 'cancelled',
        error_code: 'RUN_CANCELLED',
        error: {
          code: 'RUN_CANCELLED',
          message: '运行已由用户取消。',
          retryable: false,
          category: 'run',
          stage: 'cancellation',
        },
      },
    } as any);

    await store.cancel('run_1');

    expect(store.byId.run_1).toMatchObject({
      status: 'cancelled',
      terminal: true,
      error: {
        code: 'RUN_CANCELLED',
        retryable: false,
        category: 'run',
        stage: 'cancellation',
      },
    });
  });

  it('hydrates a terminal failure returned by cancel and clears it after success', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    vi.mocked(api.post).mockResolvedValue({
      data: {
        id: 'run_1',
        thread_id: 'thread-1',
        status: 'failed',
        error_code: 'MODEL_RATE_LIMITED',
        error: {
          code: 'MODEL_RATE_LIMITED',
          message: 'raw upstream text',
          retryable: true,
          category: 'provider',
          retry_after: 2,
        },
      },
    } as any);

    await store.cancel('run_1');
    expect(store.byId.run_1.error).toMatchObject({
      code: 'MODEL_RATE_LIMITED',
      message: '上游模型服务当前繁忙，请稍后重试',
      retryable: true,
      retryAfterSeconds: 2,
    });
    expect(store.canRetry('run_1')).toBe(true);

    const completed = store.ensure('run_2', 'thread-1');
    completed.status = 'running';
    completed.error = store.byId.run_1.error;
    store.hydrate('run_2', {
      thread_id: 'thread-1',
      status: 'succeeded',
    });
    expect(store.byId.run_2.status).toBe('completed');
    expect(store.byId.run_2.error).toBeNull();
  });

  it('does not let a stale cancel response overwrite an SSE terminal state', async () => {
    const store = useRunsStore();
    store.ensure('run_1', 'thread-1').status = 'running';
    let resolveCancel!: (value: unknown) => void;
    vi.mocked(api.post).mockImplementation(
      () => new Promise((resolve) => (resolveCancel = resolve)) as any
    );

    const cancelling = store.cancel('run_1');
    store.apply({
      schema_version: 1,
      event_id: 'evt_terminal',
      sequence: 1,
      run_id: 'run_1',
      thread_id: 'thread-1',
      type: 'run.completed',
      timestamp: '2026-07-15T00:00:00Z',
      data: { status: 'succeeded' },
    });
    resolveCancel({ data: { status: 'cancelling', thread_id: 'thread-1' } });
    await cancelling;

    expect(store.byId.run_1.status).toBe('completed');
    expect(store.byId.run_1.terminal).toBe(true);
    expect(store.byId.run_1.error).toBeNull();
  });
});
