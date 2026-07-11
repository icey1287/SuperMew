<template>
  <div class="chat-workspace">
    <section class="chat-area">
      <header class="chat-header">
        <div class="header-info">
          <h1>{{ sessionTitle }}</h1>
          <span class="header-status-line">
            <span class="status-dot"></span>
            <span>{{ generationStatus }}</span>
            <span>·</span>
            <span>上下文已同步</span>
          </span>
        </div>
        <div class="chat-header-actions">
          <button type="button" title="历史会话" aria-label="打开历史会话" @click="openHistory">
            <i class="fa-solid fa-clock-rotate-left"></i>
          </button>
          <button type="button" title="清空当前对话" aria-label="清空当前对话" @click="chatStore.handleClearChat">
            <i class="fa-regular fa-trash-can"></i>
          </button>
        </div>
      </header>

      <div class="chat-container" ref="chatContainerRef">
        <WelcomeScreen v-if="chatStore.messages.length === 0" />

        <MessageItem
          v-for="(msg, index) in chatStore.messages"
          :key="index"
          :msg="msg"
          :msg-index="index"
          :ref="(el) => { if (el) messageItemRefs[index] = el; }"
          @cite-click="scrollToChunk"
        />
      </div>

      <ChatInput />
    </section>

    <KnowledgeContextPanel @cite-click="scrollToChunk" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUpdate, onMounted, ref, watch } from 'vue';
import WelcomeScreen from './WelcomeScreen.vue';
import MessageItem from './MessageItem.vue';
import ChatInput from './ChatInput.vue';
import KnowledgeContextPanel from './KnowledgeContextPanel.vue';
import { useChatStore } from '@/stores/chat';
import { useSessionStore } from '@/stores/sessions';

const chatStore = useChatStore();
const sessionStore = useSessionStore();
const chatContainerRef = ref<HTMLDivElement | null>(null);
const messageItemRefs = ref<any[]>([]);

const sessionTitle = computed(() => {
  const session = sessionStore.sessions.find((item) => item.session_id === chatStore.sessionId);
  if (session?.title) return session.title;
  const firstUserMessage = chatStore.messages.find((message) => message.isUser && message.text.trim());
  if (!firstUserMessage) return '新对话';
  const text = firstUserMessage.text.trim();
  return text.length > 28 ? text.slice(0, 28) + '…' : text;
});

const generationStatus = computed(() => {
  if (chatStore.isViewingStreamingSession) return '喵喵正在生成';
  if (chatStore.currentPendingHitl) return '等待你的补充';
  return '喵喵在线';
});

onBeforeUpdate(() => {
  messageItemRefs.value = [];
});

const scrollToBottom = () => {
  if (chatContainerRef.value) {
    chatContainerRef.value.scrollTop = chatContainerRef.value.scrollHeight;
  }
};

const scrollToChunk = async (msgIndex: number, chunkIndex: number) => {
  const msgItem = messageItemRefs.value[msgIndex];
  if (!msgItem) return;

  msgItem.openReferences();
  await nextTick();

  const chunkEl = document.getElementById('chunk-' + msgIndex + '-' + chunkIndex);
  if (chunkEl) {
    chunkEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    chunkEl.classList.add('highlight-chunk');
    window.setTimeout(() => chunkEl.classList.remove('highlight-chunk'), 2000);
  }
};

const openHistory = async () => {
  chatStore.activeNav = 'history';
  sessionStore.showHistorySidebar = true;
  try {
    await sessionStore.fetchSessions();
    chatStore.mergeCachedSessionsIntoHistory();
  } catch (error: any) {
    alert(error.message);
  }
};

watch(
  () => chatStore.messages,
  () => nextTick(scrollToBottom),
  { deep: true }
);

watch(
  () => chatStore.sessionId,
  () => nextTick(scrollToBottom)
);

onMounted(scrollToBottom);
</script>
