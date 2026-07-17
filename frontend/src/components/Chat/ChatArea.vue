<template>
  <div class="chat-workspace">
    <section class="chat-area">
      <header class="chat-header">
        <div class="header-info">
          <h1>{{ threadTitle }}</h1>
          <span class="header-status-line">
            <span class="status-dot"></span>
            <span>{{ generationStatus }}</span>
            <span>·</span>
            <span>{{ contextSyncLabel }}</span>
          </span>
        </div>
        <div class="chat-header-actions">
          <button type="button" title="历史对话" aria-label="打开历史对话" @click="openHistory">
            <i class="fa-solid fa-clock-rotate-left"></i>
          </button>
          <button
            type="button"
            title="清空当前对话"
            aria-label="清空当前对话"
            :disabled="threadStore.isDeletingThread(chatStore.threadId)"
            @click="chatStore.handleClearChat"
          >
            <i class="fa-regular fa-trash-can"></i>
          </button>
        </div>
      </header>

      <div
        v-if="chatStore.currentTransportError"
        class="operation-notice operation-notice-action"
        role="status"
        aria-live="polite"
      >
        <span>
          Run 状态连接已中断：{{ chatStore.currentTransportError.message }}。后台运行状态未被改写。
        </span>
        <button type="button" @click="chatStore.reconnectCurrentRun">重新连接</button>
      </div>

      <p
        v-else-if="chatStore.threadLoadError"
        class="operation-notice"
        role="status"
        aria-live="polite"
      >
        对话加载失败：{{ chatStore.threadLoadError }}
      </p>

      <div class="chat-container" ref="chatContainerRef">
        <WelcomeScreen v-if="chatStore.messages.length === 0" />

        <div v-if="chatStore.hasOlderMessages" class="older-messages-control">
          <button
            type="button"
            :disabled="chatStore.isLoadingOlderMessages"
            @click="loadOlderMessages"
          >
            <i
              class="fa-solid"
              :class="
                chatStore.isLoadingOlderMessages ? 'fa-spinner fa-spin' : 'fa-clock-rotate-left'
              "
            ></i>
            {{ chatStore.isLoadingOlderMessages ? '正在加载' : '加载更早消息' }}
          </button>
        </div>

        <MessageItem
          v-for="(msg, index) in chatStore.messages"
          :key="messageKey(msg, index)"
          :msg="msg"
          :msg-index="index"
          :ref="
            (el) => {
              if (el) messageItemRefs[index] = el;
            }
          "
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
import { useThreadStore } from '@/stores/threads';
import type { Message } from '@/types/chat';

const chatStore = useChatStore();
const threadStore = useThreadStore();
const chatContainerRef = ref<HTMLDivElement | null>(null);
const messageItemRefs = ref<any[]>([]);
const preservingOlderScroll = ref(false);

const threadTitle = computed(() => {
  const thread = threadStore.threads.find((item) => item.thread_id === chatStore.threadId);
  if (thread?.title) return thread.title;
  const firstUserMessage = chatStore.messages.find(
    (message) => message.isUser && message.text.trim()
  );
  if (!firstUserMessage) return '新对话';
  const text = firstUserMessage.text.trim();
  return text.length > 28 ? text.slice(0, 28) + '…' : text;
});

const generationStatus = computed(() => {
  if (chatStore.isCreatingThread) return '正在创建对话';
  if (chatStore.loadingThreadId && chatStore.loadingThreadId === chatStore.threadId) {
    return '正在加载对话';
  }
  if (chatStore.isResumingHitl) return '正在提交补充';
  if (chatStore.currentTransportStatus === 'reconnecting') return '连接恢复中';
  if (chatStore.currentTransportError) return '运行状态连接中断';
  if (chatStore.currentRunStatus === 'creating') return '正在创建运行';
  if (['queued', 'pending'].includes(chatStore.currentRunStatus || '')) return '运行已排队';
  if (chatStore.currentRunStatus === 'cancelling') return '正在终止运行';
  if (chatStore.currentRunStatus === 'running') return '喵喵正在生成';
  if (chatStore.currentRunStatus === 'waiting_input') return '等待你的补充';
  if (chatStore.currentPendingHitl) return '等待你的补充';
  return '喵喵在线';
});

const contextSyncLabel = computed(() => {
  if (chatStore.currentTransportError) return '上下文待重新同步';
  if (chatStore.loadingThreadId && chatStore.loadingThreadId === chatStore.threadId) {
    return '上下文同步中';
  }
  return '上下文已同步';
});

const messageKey = (message: Message, index: number) => {
  if (message.id) return `message-${message.id}`;
  if (message.runId) return `run-${message.runId}-${message.isUser ? 'user' : 'assistant'}`;
  return `local-${message.sequence || index}-${message.isUser ? 'user' : 'assistant'}`;
};

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
  threadStore.showHistorySidebar = true;
  try {
    await threadStore.fetchThreads();
    chatStore.mergeCachedThreadsIntoHistory();
  } catch (error: any) {
    alert(error.message);
  }
};

const loadOlderMessages = async () => {
  const container = chatContainerRef.value;
  const threadId = chatStore.threadId;
  const previousHeight = container?.scrollHeight || 0;
  const previousTop = container?.scrollTop || 0;
  preservingOlderScroll.value = true;
  try {
    await chatStore.loadOlderMessages();
    await nextTick();
    if (container && chatStore.threadId === threadId) {
      container.scrollTop = previousTop + Math.max(container.scrollHeight - previousHeight, 0);
    }
  } catch (error: any) {
    alert(error.message);
  } finally {
    preservingOlderScroll.value = false;
  }
};

watch(
  () => chatStore.messages,
  () => {
    if (!preservingOlderScroll.value) nextTick(scrollToBottom);
  },
  { deep: true }
);

watch(
  () => chatStore.threadId,
  () => nextTick(scrollToBottom)
);

onMounted(scrollToBottom);
</script>
