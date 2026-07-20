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
            <span class="auth-mini-logo" aria-hidden="true">
              <img :src="superMewMark" class="brand-mark-image" alt="" />
            </span>
            <i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i>
            <h1>正在恢复登录状态</h1>
            <p>正在安全连接你的私有知识空间…</p>
          </div>
        </section>

        <AuthPanel v-else-if="!authStore.isAuthenticated" />

        <template v-else>
          <DocumentSettings v-if="chatStore.activeNav === 'settings'" />
          <ModelCenter v-else-if="chatStore.activeNav === 'models'" />
          <RagEvaluationWorkbench v-else-if="chatStore.activeNav === 'evaluations'" />
          <template v-else>
            <HistorySidebar />
            <ChatArea />
          </template>
          <CapabilityCenter />
          <CommandPalette />
          <ApprovalDialog />
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
import { useThreadStore } from '@/stores/threads';
import { useRunsStore } from '@/stores/runs';
import { useCapabilityStore } from '@/stores/capabilities';
import { useModelStore } from '@/stores/models';
import { useEvaluationStore } from '@/stores/evaluations';
import superMewMark from '@/assets/images/supermew-mark.png';

const HistorySidebar = defineAsyncComponent(() => import('@/components/HistorySidebar.vue'));
const ChatArea = defineAsyncComponent(() => import('@/components/Chat/ChatArea.vue'));
const DocumentSettings = defineAsyncComponent(
  () => import('@/components/Documents/DocumentSettings.vue')
);
const ModelCenter = defineAsyncComponent(() => import('@/components/Models/ModelCenter.vue'));
const RagEvaluationWorkbench = defineAsyncComponent(
  () => import('@/components/Evaluations/RagEvaluationWorkbench.vue')
);
const CapabilityCenter = defineAsyncComponent(
  () => import('@/components/Capabilities/CapabilityCenter.vue')
);
const CommandPalette = defineAsyncComponent(
  () => import('@/components/Capabilities/CommandPalette.vue')
);
const ApprovalDialog = defineAsyncComponent(
  () => import('@/components/Capabilities/ApprovalDialog.vue')
);

const authStore = useAuthStore();
const chatStore = useChatStore();
const threadStore = useThreadStore();
const runsStore = useRunsStore();
const capabilityStore = useCapabilityStore();
const modelStore = useModelStore();
const evaluationStore = useEvaluationStore();

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
    threadStore.$reset();
    modelStore.reset();
    evaluationStore.reset();
    if (username) void capabilityStore.fetchCatalog().catch(() => undefined);
  }
);

const handleUnauthorized = () => {
  authStore.clearSession();
  alert('登录已过期，请重新登录');
};

const handleGlobalShortcut = (event: KeyboardEvent) => {
  if (!authStore.isAuthenticated) return;
  if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k') {
    event.preventDefault();
    if (capabilityStore.approvalOpen) return;
    capabilityStore.togglePalette();
  }
};

const handleCapabilitySelected = () => {
  chatStore.activeNav = 'newChat';
  threadStore.showHistorySidebar = false;
};

onMounted(async () => {
  window.addEventListener('unauthorized', handleUnauthorized);
  window.addEventListener('keydown', handleGlobalShortcut);
  window.addEventListener('capability-selected', handleCapabilitySelected);
  await authStore.restoreSession();
  if (authStore.isAuthenticated && !capabilityStore.catalog && !capabilityStore.loading) {
    void capabilityStore.fetchCatalog().catch(() => undefined);
  }
});

onUnmounted(() => {
  window.removeEventListener('unauthorized', handleUnauthorized);
  window.removeEventListener('keydown', handleGlobalShortcut);
  window.removeEventListener('capability-selected', handleCapabilitySelected);
  runsStore.disconnectAll();
  evaluationStore.stopPolling();
});
</script>
