import { defineStore } from 'pinia';
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
  },
});
