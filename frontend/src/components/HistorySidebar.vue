<template>
  <div v-if="sessionStore.showHistorySidebar" class="history-backdrop" @click.self="closeHistory">
    <aside class="history-sidebar">
      <div class="history-header">
        <div>
          <span class="panel-eyebrow">Conversation memory</span>
          <h2>历史会话</h2>
        </div>
        <button type="button" class="close-btn" aria-label="关闭历史会话" @click="closeHistory">
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>

      <div class="history-summary">
        <span><strong>{{ sessionStore.sessions.length }}</strong> 个会话</span>
        <button type="button" @click="refreshSessions">
          <i class="fa-solid fa-rotate" :class="{ 'fa-spin': refreshing }"></i>
          刷新
        </button>
      </div>

      <div class="history-list">
        <div v-if="sessionStore.sessions.length === 0" class="empty-history">
          <span class="empty-icon"><i class="fa-regular fa-comments"></i></span>
          <h3>暂无历史记录</h3>
          <p>开始一段新对话后，喵喵会在这里替你保存。</p>
        </div>

        <article
          v-for="session in sessionStore.sessions"
          :key="session.session_id"
          :class="['history-item', { active: session.session_id === chatStore.sessionId }]"
        >
          <button type="button" class="session-body" @click="onLoadSession(session.session_id)">
            <span class="session-state-dot" aria-hidden="true"></span>
            <span class="session-info">
              <strong class="session-title">{{ session.title || '未命名会话' }}</strong>
              <span class="session-meta">
                <span>{{ session.message_count }} 条消息</span>
                <span v-if="session.isStreaming" class="session-status">生成中</span>
                <span>{{ formatDate(session.updated_at) }}</span>
              </span>
            </span>
          </button>
          <button
            type="button"
            class="history-delete-btn"
            title="删除会话"
            aria-label="删除会话"
            @click.stop="onDeleteSession(session.session_id)"
          >
            <i class="fa-regular fa-trash-can"></i>
          </button>
        </article>
      </div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { useChatStore } from '@/stores/chat';
import { useSessionStore } from '@/stores/sessions';

const chatStore = useChatStore();
const sessionStore = useSessionStore();
const refreshing = ref(false);

const closeHistory = () => {
  sessionStore.showHistorySidebar = false;
  if (chatStore.activeNav === 'history') {
    chatStore.activeNav = 'newChat';
  }
};

const refreshSessions = async () => {
  refreshing.value = true;
  try {
    await sessionStore.fetchSessions();
    chatStore.mergeCachedSessionsIntoHistory();
  } catch (error: any) {
    alert(error.message);
  } finally {
    refreshing.value = false;
  }
};

const onLoadSession = async (sessionId: string) => {
  try {
    await chatStore.loadSession(sessionId);
  } catch (error: any) {
    alert('加载会话失败：' + error.message);
  }
};

const onDeleteSession = async (sessionId: string) => {
  if (chatStore.streamingSessionId === sessionId) {
    alert('该会话正在生成回答，请先终止或等待完成后再删除');
    return;
  }

  const sessionLabel = sessionStore.sessions.find((session) => session.session_id === sessionId)?.title || sessionId;
  if (!confirm('确定要删除会话“' + sessionLabel + '”吗？')) {
    return;
  }

  try {
    await sessionStore.deleteSession(sessionId);
    delete chatStore.messagesBySession[sessionId];
    if (chatStore.sessionId === sessionId) {
      chatStore.handleNewChat();
    } else {
      chatStore.mergeCachedSessionsIntoHistory();
    }
  } catch (error: any) {
    alert('删除会话失败：' + error.message);
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
