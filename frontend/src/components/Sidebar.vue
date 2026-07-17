<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="logo-icon" aria-hidden="true">
        <i class="fa-solid fa-cat"></i>
      </div>
      <div class="brand-copy">
        <h1>喵喵助手</h1>
        <span>Knowledge Copilot</span>
      </div>
    </div>

    <div class="workspace-switcher">
      <span class="workspace-orb" aria-hidden="true"></span>
      <span class="workspace-copy">
        <strong>SuperMew 知识空间</strong>
        <small>{{ workspaceMeta }}</small>
      </span>
      <i class="fa-solid fa-chevron-down" aria-hidden="true"></i>
    </div>

    <nav class="sidebar-nav" aria-label="主导航">
      <button
        type="button"
        :class="['nav-btn', { active: chatStore.activeNav === 'newChat' }]"
        aria-label="智能对话"
        @click="onNewChat"
      >
        <i class="fa-regular fa-message"></i>
        <span>智能对话</span>
      </button>
      <button
        type="button"
        :class="['nav-btn', { active: chatStore.activeNav === 'history' }]"
        aria-label="历史对话"
        @click="onHistory"
      >
        <i class="fa-solid fa-clock-rotate-left"></i>
        <span>历史对话</span>
        <small v-if="threadStore.threads.length" class="nav-count">
          {{ threadStore.threads.length }}
        </small>
      </button>
      <button
        v-if="authStore.isAdmin"
        type="button"
        :class="['nav-btn', { active: chatStore.activeNav === 'settings' }]"
        aria-label="知识库"
        @click="onSettings"
      >
        <i class="fa-regular fa-bookmark"></i>
        <span>知识库</span>
      </button>
    </nav>

    <template v-if="authStore.isAuthenticated">
      <div class="sidebar-section-label">最近对话</div>
      <div class="sidebar-recents">
        <button
          v-for="thread in recentThreads"
          :key="thread.thread_id"
          type="button"
          :class="['recent-thread', { active: thread.thread_id === chatStore.threadId }]"
          @click="onLoadThread(thread.thread_id)"
        >
          <span class="recent-dot" aria-hidden="true"></span>
          <span class="recent-copy">
            <strong>{{ thread.title || '未命名对话' }}</strong>
            <small>
              {{ threadStatusLabel(thread) }}
              · {{ formatRelativeTime(thread.updated_at) }}
            </small>
          </span>
        </button>

        <p v-if="threadStore.historyError" class="sidebar-operation-notice" role="status">
          对话同步失败：{{ threadStore.historyError }}
        </p>

        <div v-if="!recentThreads.length" class="recent-empty">
          还没有历史对话，问喵喵一个问题吧。
        </div>
      </div>
    </template>

    <div class="sidebar-bottom">
      <div class="theme-control">
        <span class="theme-control-label">
          <i :class="theme === 'light' ? 'fa-regular fa-sun' : 'fa-regular fa-moon'"></i>
          <span>{{ theme === 'light' ? '浅色模式' : '深色模式' }}</span>
        </span>
        <ThemeToggle :theme="theme" @toggle="$emit('toggle-theme')" />
      </div>

      <div v-if="authStore.isAuthenticated" class="user-panel">
        <span class="user-avatar">{{ userInitials }}</span>
        <span class="user-copy">
          <strong>{{ authStore.currentUser?.username }}</strong>
          <small>{{ roleLabel }}</small>
        </span>
        <span class="user-actions">
          <button
            type="button"
            title="清空当前对话"
            aria-label="清空当前对话"
            :disabled="threadStore.isDeletingThread(chatStore.threadId)"
            @click="chatStore.handleClearChat"
          >
            <i class="fa-regular fa-trash-can"></i>
          </button>
          <button
            type="button"
            title="退出登录"
            aria-label="退出登录"
            :disabled="authStore.authLoading"
            @click="onLogout"
          >
            <i
              :class="
                authStore.authLoading
                  ? 'fa-solid fa-spinner fa-spin'
                  : 'fa-solid fa-arrow-right-from-bracket'
              "
            ></i>
          </button>
        </span>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue';
import ThemeToggle from '@/components/ThemeToggle.vue';
import { useAuthStore } from '@/stores/auth';
import { useChatStore } from '@/stores/chat';
import { useThreadStore } from '@/stores/threads';
import type { ThreadListItem } from '@/types/threads';

defineProps<{
  theme: 'dark' | 'light';
}>();

defineEmits<{
  (e: 'toggle-theme'): void;
}>();

const authStore = useAuthStore();
const chatStore = useChatStore();
const threadStore = useThreadStore();

const recentThreads = computed(() => threadStore.threads.slice(0, 4));

const workspaceMeta = computed(() => {
  if (!authStore.isAuthenticated) return '登录后连接私有知识';
  return (threadStore.threads.length || 0) + ' 个对话 · 私有';
});

const roleLabel = computed(() => (authStore.currentUser?.role === 'admin' ? '管理员' : '普通用户'));

const userInitials = computed(() => {
  const name = authStore.currentUser?.username || 'ME';
  return name.slice(0, 2).toUpperCase();
});

const refreshThreads = async () => {
  if (!authStore.isAuthenticated) return;
  try {
    await threadStore.fetchThreads();
    chatStore.mergeCachedThreadsIntoHistory();
  } catch {
    threadStore.historyError ||= '最近对话同步失败，请稍后重试';
  }
};

watch(
  () => authStore.isAuthenticated,
  (isAuthenticated) => {
    if (isAuthenticated) refreshThreads();
  },
  { immediate: true }
);

const onNewChat = () => {
  void chatStore.handleNewChat();
};

const onHistory = async () => {
  chatStore.activeNav = 'history';
  threadStore.showHistorySidebar = !threadStore.showHistorySidebar;
  if (threadStore.showHistorySidebar) {
    try {
      await threadStore.fetchThreads();
      chatStore.mergeCachedThreadsIntoHistory();
    } catch (error: any) {
      alert(error.message);
    }
  }
};

const onSettings = () => {
  if (!authStore.isAdmin) {
    alert('仅管理员可访问文档管理');
    return;
  }
  chatStore.activeNav = 'settings';
  threadStore.showHistorySidebar = false;
};

const onLoadThread = async (threadId: string) => {
  try {
    await chatStore.loadThread(threadId);
  } catch (error: any) {
    alert('加载对话失败：' + error.message);
  }
};

const onLogout = async () => {
  threadStore.showHistorySidebar = false;
  await authStore.handleLogout();
};

const threadStatusLabel = (thread: ThreadListItem) => {
  if (thread.activeRunStatus === 'waiting_input') return '等待补充';
  if (thread.activeRunStatus === 'cancelling') return '终止中';
  if (thread.isStreaming) return '生成中';
  return `${thread.message_count} 条消息`;
};

const formatRelativeTime = (value: string) => {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '刚刚';
  const diffMinutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
  if (diffMinutes < 1) return '刚刚';
  if (diffMinutes < 60) return diffMinutes + ' 分钟前';
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return diffHours + ' 小时前';
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return diffDays + ' 天前';
  return new Date(value).toLocaleDateString();
};
</script>
