import { defineStore } from 'pinia';
import api from '@/utils/api';
import {
  applyRunEvent,
  initialRunEventState,
  type RunEventState,
} from '@/events/runEventReducer';
import type { RunEventV1 } from '@/types/generated/run-event-v1';

export const useRunsStore = defineStore('runs', {
  state: () => ({
    byId: {} as Record<string, RunEventState>,
  }),
  getters: {
    activeForThread: (state) => (threadId: string | null) => {
      if (!threadId) return null;
      return Object.values(state.byId).find(
        (run) =>
          run.threadId === threadId &&
          !run.terminal &&
          !['idle', 'cancelled', 'failed', 'completed'].includes(run.status)
      ) || null;
    },
  },
  actions: {
    ensure(runId: string, threadId: string): RunEventState {
      if (!this.byId[runId]) {
        this.byId[runId] = initialRunEventState(runId, threadId);
      }
      return this.byId[runId];
    },
    apply(event: RunEventV1) {
      const current = this.ensure(event.run_id, event.thread_id);
      this.byId[event.run_id] = applyRunEvent(current, event);
    },
    remove(runId: string) {
      delete this.byId[runId];
    },
    async cancel(runId: string) {
      const current = this.byId[runId];
      if (current && !current.terminal) current.status = 'cancelling';
      const response = await api.post(`/v1/runs/${encodeURIComponent(runId)}/cancel`);
      const status = String(response.data?.status || 'cancelling');
      if (current) {
        current.status = status === 'succeeded' ? 'completed' : (status as RunEventState['status']);
        current.terminal = ['succeeded', 'completed', 'failed', 'cancelled'].includes(status);
      }
      return response.data;
    },
  },
});
