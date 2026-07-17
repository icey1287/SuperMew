import { defineStore } from 'pinia';
import { createThread, deleteThread, listThreads } from '@/threads/threadClient';
import type { ThreadDetail, ThreadListItem, ThreadSummary } from '@/types/threads';
import { getPublicError } from '@/utils/api';

const BUSY_RUN_STATUSES = new Set([
  'creating',
  'queued',
  'pending',
  'running',
  'waiting_input',
  'cancelling',
]);

function toThreadListItem(thread: ThreadSummary | ThreadDetail): ThreadListItem {
  const activeRunId = thread.active_run_id;
  const activeRunStatus = thread.active_run_status;
  return {
    ...thread,
    activeRunId,
    activeRunStatus,
    isStreaming: BUSY_RUN_STATUSES.has(String(activeRunStatus || '')),
  };
}

export const useSessionStore = defineStore('sessions', {
  state: () => ({
    sessions: [] as ThreadListItem[],
    showHistorySidebar: false,
  }),

  getters: {
    threadById: (state) => (threadId: string) =>
      state.sessions.find((thread) => thread.thread_id === threadId) || null,
  },

  actions: {
    async fetchSessions() {
      try {
        this.sessions = (await listThreads()).map(toThreadListItem);
      } catch (error) {
        throw getPublicError(error);
      }
    },

    async createSession(title?: string) {
      try {
        const created = await createThread(title ? { title } : {});
        const item = toThreadListItem(created);
        this.sessions = [
          item,
          ...this.sessions.filter((thread) => thread.thread_id !== item.thread_id),
        ];
        return item;
      } catch (error) {
        throw getPublicError(error);
      }
    },

    async deleteSession(sessionId: string) {
      try {
        const response = await deleteThread(sessionId);
        this.sessions = this.sessions.filter((thread) => thread.thread_id !== sessionId);
        return response.message || '会话已删除';
      } catch (error) {
        throw getPublicError(error);
      }
    },

    setRunView(threadId: string, runId: string | null, status: string | null) {
      const thread = this.sessions.find((item) => item.thread_id === threadId);
      if (!thread) return;
      thread.activeRunId = runId;
      thread.activeRunStatus = status;
      thread.isStreaming = BUSY_RUN_STATUSES.has(String(status || ''));
    },
  },
});
