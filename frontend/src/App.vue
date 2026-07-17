<template>
  <div class="app-page">
    <div class="aurora-orb aurora-orb-one" aria-hidden="true"></div>
    <div class="aurora-orb aurora-orb-two" aria-hidden="true"></div>

    <div class="app-wrapper">
      <Sidebar :theme="theme" @toggle-theme="toggleTheme" />

      <main class="main-content">
        <section
          v-if="!authStore.authResolved"
          class="auth-page auth-restore-page"
          role="status"
          aria-live="polite"
        >
          <div class="auth-panel auth-restore-panel">
            <span class="auth-mini-logo"><i class="fa-solid fa-cat"></i></span>
            <i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i>
            <h1>正在恢复登录状态</h1>
            <p>正在安全连接你的私有知识空间…</p>
          </div>
        </section>

        <AuthPanel v-else-if="!authStore.isAuthenticated" />

        <template v-else>
          <DocumentSettings v-if="chatStore.activeNav === 'settings'" />
          <HistorySidebar />
          <ChatArea v-show="chatStore.activeNav !== 'settings'" />
        </template>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue';
import Sidebar from '@/components/Sidebar.vue';
import AuthPanel from '@/components/AuthPanel.vue';

import { useAuthStore } from '@/stores/auth';
import { useChatStore } from '@/stores/chat';
import { useSessionStore } from '@/stores/sessions';
import { useRunsStore } from '@/stores/runs';

const HistorySidebar = defineAsyncComponent(() => import('@/components/HistorySidebar.vue'));
const ChatArea = defineAsyncComponent(() => import('@/components/Chat/ChatArea.vue'));
const DocumentSettings = defineAsyncComponent(
  () => import('@/components/Documents/DocumentSettings.vue')
);

const authStore = useAuthStore();
const chatStore = useChatStore();
const sessionStore = useSessionStore();
const runsStore = useRunsStore();

type Theme = 'dark' | 'light';

const storedTheme = localStorage.getItem('supermew-theme');
const theme = ref<Theme>(storedTheme === 'light' ? 'light' : 'dark');

const applyTheme = (nextTheme: Theme) => {
  document.documentElement.dataset.theme = nextTheme;
  document.documentElement.style.colorScheme = nextTheme;
  localStorage.setItem('supermew-theme', nextTheme);
};

const toggleTheme = () => {
  theme.value = theme.value === 'dark' ? 'light' : 'dark';
};

watch(theme, applyTheme, { immediate: true });

watch(
  () => authStore.currentUser?.username || null,
  (username, previousUsername) => {
    if (username === previousUsername) return;
    chatStore.resetWorkspace();
    sessionStore.$reset();
  }
);

const handleUnauthorized = () => {
  authStore.clearSession();
  alert('登录已过期，请重新登录');
};

onMounted(async () => {
  window.addEventListener('unauthorized', handleUnauthorized);
  await authStore.restoreSession();
});

onUnmounted(() => {
  window.removeEventListener('unauthorized', handleUnauthorized);
  runsStore.disconnectAll();
});
</script>
