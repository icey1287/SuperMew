<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="logo-icon">🐱</div>
      <h2>喵喵助手</h2>
    </div>
    <nav class="sidebar-nav">
      <button @click="onNewChat" :class="['nav-btn', { active: chatStore.activeNav === 'newChat' }]">
        <i class="fas fa-plus"></i> 新建会话
      </button>
      <button @click="onHistory" :class="['nav-btn', { active: chatStore.activeNav === 'history' }]">
        <i class="fas fa-history"></i> 历史记录
      </button>
      <button v-if="authStore.isAdmin" @click="onSettings" :class="['nav-btn', { active: chatStore.activeNav === 'settings' }]">
        <i class="fas fa-cog"></i> 设置
      </button>
    </nav>
    <div class="sidebar-footer">
      <button @click="chatStore.handleClearChat" class="danger-btn">
        <i class="fas fa-trash-alt"></i> 清空当前对话
      </button>
      <div v-if="authStore.isAuthenticated" class="user-badge">
        <span>{{ authStore.currentUser?.username }}</span>
        <small>{{ authStore.currentUser?.role }}</small>
      </div>
      <button v-if="authStore.isAuthenticated" @click="authStore.handleLogout" class="danger-btn logout-btn">
        <i class="fas fa-right-from-bracket"></i> 退出登录
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { useAuthStore } from '@/stores/auth';
import { useChatStore } from '@/stores/chat';
import { useSessionStore } from '@/stores/sessions';

const authStore = useAuthStore();
const chatStore = useChatStore();
const sessionStore = useSessionStore();

const onNewChat = () => {
  chatStore.handleNewChat();
};

const onHistory = async () => {
  chatStore.activeNav = 'history';
  sessionStore.showHistorySidebar = !sessionStore.showHistorySidebar;
  if (sessionStore.showHistorySidebar) {
    try {
      await sessionStore.fetchSessions();
      chatStore.mergeCachedSessionsIntoHistory();
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
  sessionStore.showHistorySidebar = false;
};
</script>
