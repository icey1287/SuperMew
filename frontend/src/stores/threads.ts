import { defineStore } from 'pinia';
import {
  createThread as createThreadRequest,
  deleteThread as deleteThreadRequest,
  listThreads,
} from '@/threads/threadClient';
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

export const useThreadStore = defineStore('threads', {
  state: () => ({
    threads: [] as ThreadListItem[],
    showHistorySidebar: false,
    historyLoading: false,
    historyError: '',
    deletingThreadIds: {} as Record<string, boolean>,
  }),

  getters: {
    threadById: (state) => (threadId: string) =>
      state.threads.find((thread) => thread.thread_id === threadId) || null,
    isDeletingThread: (state) => (threadId: string) => Boolean(state.deletingThreadIds[threadId]),
  },

  actions: {
    async fetchThreads() {
      this.historyLoading = true;
      this.historyError = '';
      try {
        this.threads = (await listThreads()).map(toThreadListItem);
      } catch (error) {
        const publicError = getPublicError(error);
        this.historyError = publicError.message;
        throw publicError;
      } finally {
        this.historyLoading = false;
      }
    },

    async createThread(title?: string) {
      try {
        const created = await createThreadRequest(title ? { title } : {});
        const item = toThreadListItem(created);
        this.threads = [
          item,
          ...this.threads.filter((thread) => thread.thread_id !== item.thread_id),
        ];
        return item;
      } catch (error) {
        throw getPublicError(error);
      }
    },

    async deleteThread(threadId: string) {
      if (this.deletingThreadIds[threadId]) return null;
      this.deletingThreadIds = { ...this.deletingThreadIds, [threadId]: true };
      try {
        const response = await deleteThreadRequest(threadId);
        this.threads = this.threads.filter((thread) => thread.thread_id !== threadId);
        return response.message || '对话已删除';
      } catch (error) {
        throw getPublicError(error);
      } finally {
        const { [threadId]: _finished, ...remaining } = this.deletingThreadIds;
        this.deletingThreadIds = remaining;
      }
    },

    setRunView(threadId: string, runId: string | null, status: string | null) {
      const thread = this.threads.find((item) => item.thread_id === threadId);
      if (!thread) return;
      thread.activeRunId = runId;
      thread.activeRunStatus = status;
      thread.isStreaming = BUSY_RUN_STATUSES.has(String(status || ''));
    },
  },
});
