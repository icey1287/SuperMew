<template>
  <div v-if="threadStore.showHistorySidebar" class="history-backdrop" @click.self="closeHistory">
    <aside class="history-sidebar">
      <div class="history-header">
        <div>
          <span class="panel-eyebrow">Conversation memory</span>
          <h2>历史对话</h2>
        </div>
        <button type="button" class="close-btn" aria-label="关闭历史对话" @click="closeHistory">
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>

      <div class="history-summary">
        <span
          ><strong>{{ threadStore.threads.length }}</strong> 个对话</span
        >
        <button type="button" :disabled="threadStore.historyLoading" @click="refreshThreads">
          <i class="fa-solid fa-rotate" :class="{ 'fa-spin': threadStore.historyLoading }"></i>
          刷新
        </button>
      </div>

      <p v-if="threadStore.historyError" class="operation-notice" role="status" aria-live="polite">
        历史对话同步失败：{{ threadStore.historyError }}
      </p>

      <div class="history-list">
        <div v-if="threadStore.threads.length === 0" class="empty-history">
          <span class="empty-icon"><i class="fa-regular fa-comments"></i></span>
          <h3>暂无历史记录</h3>
          <p>开始一段新对话后，喵喵会在这里替你保存。</p>
        </div>

        <article
          v-for="thread in threadStore.threads"
          :key="thread.thread_id"
          :class="['history-item', { active: thread.thread_id === chatStore.threadId }]"
        >
          <button type="button" class="thread-body" @click="onLoadThread(thread.thread_id)">
            <span class="thread-state-dot" aria-hidden="true"></span>
            <span class="thread-info">
              <strong class="thread-title">{{ thread.title || '未命名对话' }}</strong>
              <span class="thread-meta">
                <span>{{ thread.message_count }} 条消息</span>
                <span v-if="thread.activeRunStatus === 'waiting_input'" class="thread-status"
                  >等待补充</span
                >
                <span v-else-if="thread.activeRunStatus === 'cancelling'" class="thread-status"
                  >终止中</span
                >
                <span v-else-if="thread.isStreaming" class="thread-status">生成中</span>
                <span>{{ formatDate(thread.updated_at) }}</span>
              </span>
            </span>
          </button>
          <button
            type="button"
            class="history-delete-btn"
            title="删除对话"
            aria-label="删除对话"
            :disabled="threadStore.isDeletingThread(thread.thread_id)"
            @click.stop="onDeleteThread(thread.thread_id)"
          >
            <i
              :class="
                threadStore.isDeletingThread(thread.thread_id)
                  ? 'fa-solid fa-spinner fa-spin'
                  : 'fa-regular fa-trash-can'
              "
            ></i>
          </button>
        </article>
      </div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { useChatStore } from '@/stores/chat';
import { useThreadStore } from '@/stores/threads';
import { useRunsStore } from '@/stores/runs';

const chatStore = useChatStore();
const threadStore = useThreadStore();
const runsStore = useRunsStore();

const closeHistory = () => {
  threadStore.showHistorySidebar = false;
  if (chatStore.activeNav === 'history') {
    chatStore.activeNav = 'newChat';
  }
};

const refreshThreads = async () => {
  try {
    await threadStore.fetchThreads();
    chatStore.mergeCachedThreadsIntoHistory();
  } catch {
    threadStore.historyError ||= '历史对话同步失败，请稍后重试';
  }
};

const onLoadThread = async (threadId: string) => {
  try {
    await chatStore.loadThread(threadId);
  } catch (error: any) {
    alert('加载对话失败：' + error.message);
  }
};

const onDeleteThread = async (threadId: string) => {
  if (runsStore.activeForThread(threadId) || threadStore.threadById(threadId)?.isStreaming) {
    alert('该对话仍有活跃运行，请先终止或等待完成后再删除');
    return;
  }

  const threadLabel =
    threadStore.threads.find((thread) => thread.thread_id === threadId)?.title || threadId;
  if (!confirm('确定要删除对话“' + threadLabel + '”吗？')) {
    return;
  }

  try {
    await threadStore.deleteThread(threadId);
    chatStore.removeThreadState(threadId);
    if (chatStore.threadId === threadId) {
      chatStore.handleNewChat();
    } else {
      chatStore.mergeCachedThreadsIntoHistory();
    }
  } catch (error: any) {
    alert('删除对话失败：' + error.message);
  }
};

const formatDate = (value: string) => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '刚刚';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
};
</script>
