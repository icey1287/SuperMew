<template>
  <div v-if="sessionStore.showHistorySidebar" class="history-sidebar">
    <div class="history-header">
      <h3>历史会话</h3>
      <button @click="sessionStore.showHistorySidebar = false" class="close-btn">
        <i class="fas fa-times"></i>
      </button>
    </div>
    <div class="history-list">
      <div v-if="sessionStore.sessions.length === 0" class="empty-history">
        <p>暂无历史记录</p>
      </div>
      <div 
        v-for="session in sessionStore.sessions" 
        :key="session.session_id"
        class="history-item"
        :class="{ active: session.session_id === chatStore.sessionId }"
      >
        <div class="session-body" @click="onLoadSession(session.session_id)">
          <div class="session-info">
            <div class="session-title">{{ session.title || session.session_id }}</div>
            <div class="session-meta">
              <span>{{ session.message_count }} 条消息</span>
              <span v-if="session.isStreaming" class="session-status">生成中</span>
              <span>{{ new Date(session.updated_at).toLocaleString() }}</span>
            </div>
          </div>
        </div>
        <button 
          class="history-delete-btn"
          title="删除会话"
          @click.stop="onDeleteSession(session.session_id)"
        >
          <i class="fas fa-trash"></i>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useChatStore } from '@/stores/chat';
import { useSessionStore } from '@/stores/sessions';

const chatStore = useChatStore();
const sessionStore = useSessionStore();

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

  const sessionLabel = sessionStore.sessions.find(s => s.session_id === sessionId)?.title || sessionId;
  if (!confirm(`确定要删除会话 "${sessionLabel}" 吗？`)) {
    return;
  }

  try {
    const successMsg = await sessionStore.deleteSession(sessionId);
    delete chatStore.messagesBySession[sessionId];
    if (chatStore.sessionId === sessionId) {
      chatStore.handleNewChat();
    } else {
      chatStore.mergeCachedSessionsIntoHistory();
    }
    alert(successMsg);
  } catch (error: any) {
    alert('删除会话失败：' + error.message);
  }
};
</script>
