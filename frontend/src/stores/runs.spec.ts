import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import { useRunsStore } from './runs';

vi.mock('@/utils/api', () => ({
  default: {
    post: vi.fn(),
  },
}));

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
});
