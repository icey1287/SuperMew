import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import {
  cancelRun,
  createIdempotencyKey,
  createRun,
  getRun,
  getRunEvents,
  resumeRun,
} from './runClient';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: {
      get: vi.fn(),
      post: vi.fn(),
    },
  };
});

describe('run client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('creates bounded unique idempotency keys', () => {
    const first = createIdempotencyKey('run');
    const second = createIdempotencyKey('run');
    const resume = createIdempotencyKey('resume');

    expect(first).toMatch(/^run_[A-Za-z0-9_-]+$/);
    expect(second).not.toBe(first);
    expect(resume).toMatch(/^resume_[A-Za-z0-9_-]+$/);
    expect(first.length).toBeLessThanOrEqual(128);
  });

  it('creates a Run with the durable payload through the shared API client', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: {
        run: { id: 'run_1', thread_id: 'thread_one', status: 'pending' },
        created: true,
        thread_version: 2,
      },
    } as any);

    await createRun('thread_one', {
      message: 'hello',
      idempotency_key: 'run_key',
      multitask_strategy: 'reject',
      on_disconnect: 'continue',
      approved_tools: [],
    });

    expect(api.post).toHaveBeenCalledWith('/v1/threads/thread_one/runs', {
      message: 'hello',
      idempotency_key: 'run_key',
      multitask_strategy: 'reject',
      on_disconnect: 'continue',
      approved_tools: [],
    });
  });

  it('rejects an invalid Thread ID before issuing a Run request', async () => {
    await expect(
      createRun('thread/one', {
        message: 'hello',
        idempotency_key: 'run_key',
        multitask_strategy: 'reject',
        on_disconnect: 'continue',
        approved_tools: [],
      })
    ).rejects.toMatchObject({ code: 'INVALID_REQUEST' });

    expect(api.post).not.toHaveBeenCalled();
  });

  it('gets Run state and paged events through authenticated endpoints', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        data: { id: 'run_1', thread_id: 'thread-1', status: 'running' },
      } as any)
      .mockResolvedValueOnce({ data: { events: [], next_after: 7 } } as any);

    await getRun('run/1');
    await getRunEvents('run/1', { after: 7, limit: 5000 });

    expect(api.get).toHaveBeenNthCalledWith(1, '/v1/runs/run%2F1');
    expect(api.get).toHaveBeenNthCalledWith(2, '/v1/runs/run%2F1/events', {
      params: { after: 7, limit: 1000 },
    });
  });

  it('cancels and resumes the same Run without creating another one', async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({
        data: { id: 'run_1', thread_id: 'thread-1', status: 'cancelling' },
      } as any)
      .mockResolvedValueOnce({
        data: {
          run: { id: 'run_1', thread_id: 'thread-1', status: 'pending' },
          checkpoint_id: 'checkpoint_1',
          created: true,
        },
      } as any);

    await cancelRun('run_1');
    await resumeRun('run_1', {
      hitl_token: 'hitl_1',
      answer: '丹瑾',
      idempotency_key: 'resume_1',
    });

    expect(api.post).toHaveBeenNthCalledWith(1, '/v1/runs/run_1/cancel');
    expect(api.post).toHaveBeenNthCalledWith(2, '/v1/runs/run_1/resume', {
      hitl_token: 'hitl_1',
      answer: '丹瑾',
      idempotency_key: 'resume_1',
    });
  });
});
