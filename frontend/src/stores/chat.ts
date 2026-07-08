import { defineStore } from 'pinia';
import { useAuthStore } from './auth';
import { useSessionStore } from './sessions';
import api from '@/utils/api';
import type { Message, RagStep, GroupedRagStep } from '@/types/chat';

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [] as Message[],
    messagesBySession: {} as Record<string, Message[]>,
    userInput: '',
    isLoading: false,
    activeNav: 'newChat' as 'newChat' | 'history' | 'settings',
    sessionId: 'session_' + Date.now(),
    streamingSessionId: null as string | null,
    abortController: null as AbortController | null,
  }),

  getters: {
    isViewingStreamingSession(state): boolean {
      return state.isLoading && state.streamingSessionId === state.sessionId;
    },

    isInputLocked(state): boolean {
      return state.isLoading && state.streamingSessionId !== state.sessionId;
    },
  },

  actions: {
    ensureSessionMessages(sessionId: string): Message[] {
      if (!this.messagesBySession[sessionId]) {
        this.messagesBySession[sessionId] = [];
      }
      return this.messagesBySession[sessionId];
    },

    setViewedSession(sessionId: string, messages?: Message[]) {
      if (messages) {
        this.messagesBySession[sessionId] = messages;
      }
      this.sessionId = sessionId;
      this.messages = this.ensureSessionMessages(sessionId);
      this.activeNav = 'newChat';
    },

    createSessionId(): string {
      let nextId = 'session_' + Date.now();
      while (this.messagesBySession[nextId]) {
        nextId = 'session_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
      }
      return nextId;
    },

    getLocalSessionTitle(sessionId: string, messages: Message[]): string {
      const firstUserMessage = messages.find((msg) => msg.isUser && msg.text.trim());
      if (!firstUserMessage) return sessionId;
      const title = firstUserMessage.text.trim();
      return title.length > 10 ? title.substring(0, 10) + '...' : title;
    },

    mapServerMessages(messages: any[]): Message[] {
      return (messages || []).map((msg: any) => ({
        text: msg.content,
        isUser: msg.type === 'human',
        ragTrace: msg.rag_trace || null,
      }));
    },

    mergeCachedSessionsIntoHistory() {
      const sessionStore = useSessionStore();
      const sessions = sessionStore.sessions.map((session) => ({
        ...session,
        isStreaming: this.isLoading && session.session_id === this.streamingSessionId,
      }));

      Object.entries(this.messagesBySession).forEach(([sessionId, messages]) => {
        if (!messages.length) return;

        const existingIndex = sessions.findIndex((session) => session.session_id === sessionId);
        const existing = existingIndex >= 0 ? sessions[existingIndex] : null;
        const localSession = {
          session_id: sessionId,
          title: existing?.title || this.getLocalSessionTitle(sessionId, messages),
          message_count: Math.max(existing?.message_count || 0, messages.length),
          updated_at: existing?.updated_at || new Date().toISOString(),
          isStreaming: this.isLoading && sessionId === this.streamingSessionId,
        };

        if (existingIndex >= 0) {
          sessions[existingIndex] = { ...existing, ...localSession };
        } else {
          sessions.unshift(localSession);
        }
      });

      sessionStore.sessions = sessions;
    },

    appendRagStepToGroups(prev: GroupedRagStep[], step: RagStep): GroupedRagStep[] {
      const groups = prev ? [...prev] : [];
      const g = step.group || null;
      const groupLabel = step.group_label || g;
      
      if (g) {
        const idx = groups.findIndex((grp) => grp.group === g);
        if (idx >= 0) {
          const existing = groups[idx];
          const updated: GroupedRagStep = {
            group: existing.group,
            label: existing.label || groupLabel,
            steps: [...existing.steps, step],
            collapsed: existing.collapsed,
          };
          groups[idx] = updated;
          return groups;
        }
        return [...groups, { group: g, label: groupLabel, steps: [step], collapsed: true }];
      }

      const last = groups.length > 0 ? groups[groups.length - 1] : null;
      if (last && last.group === null) {
        const updated = { ...last, steps: [...last.steps, step] };
        groups[groups.length - 1] = updated;
        return groups;
      }
      return [...groups, { group: null, label: null, steps: [step], collapsed: false }];
    },

    groupRagSteps(steps: RagStep[]): GroupedRagStep[] {
      if (!steps || !steps.length) return [];
      return steps.reduce((groups: GroupedRagStep[], step) => this.appendRagStepToGroups(groups, step), []);
    },

    toggleStepGroup(msgIndex: number, groupIndex: number) {
      const msg = this.messages[msgIndex];
      if (!msg || !msg._groupedSteps || !msg._groupedSteps[groupIndex]) return;
      msg._groupedSteps[groupIndex].collapsed = !msg._groupedSteps[groupIndex].collapsed;
    },

    handleNewChat() {
      const sessionId = this.createSessionId();
      this.messagesBySession[sessionId] = [];
      this.setViewedSession(sessionId);
      const sessionStore = useSessionStore();
      sessionStore.showHistorySidebar = false;
    },

    handleClearChat() {
      if (this.streamingSessionId === this.sessionId) {
        alert('当前会话正在生成回答，请先终止或等待完成后再清空');
        return;
      }
      if (confirm('确定要清空当前对话吗？喵？')) {
        this.messagesBySession[this.sessionId] = [];
        this.messages = this.messagesBySession[this.sessionId];
      }
    },

    async loadSession(sessionId: string) {
      const sessionStore = useSessionStore();
      const cachedMessages = this.messagesBySession[sessionId];

      this.setViewedSession(sessionId, cachedMessages || []);
      sessionStore.showHistorySidebar = false;

      if (sessionId === this.streamingSessionId) {
        this.mergeCachedSessionsIntoHistory();
        return;
      }

      try {
        const response = await api.get(`/sessions/${encodeURIComponent(sessionId)}`);
        const data = response.data;
        const loadedMessages = this.mapServerMessages(data.messages || []);
        this.messagesBySession[sessionId] = loadedMessages;
        if (this.sessionId === sessionId) {
          this.messages = loadedMessages;
        }
        this.mergeCachedSessionsIntoHistory();
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '加载会话失败';
        if (!cachedMessages && this.sessionId === sessionId) {
          this.messages = [];
        }
        throw new Error(errMsg);
      }
    },

    handleStop() {
      if (this.abortController) {
        this.abortController.abort();
      }
    },

    async handleSend() {
      const authStore = useAuthStore();
      const sessionStore = useSessionStore();

      if (!authStore.isAuthenticated) {
        alert('请先登录');
        return;
      }

      const text = this.userInput.trim();
      if (!text) return;
      if (this.isLoading) {
        alert('当前已有回答正在生成，请先等待完成或回到该会话终止回答');
        return;
      }

      const requestSessionId = this.sessionId;
      const requestMessages = this.ensureSessionMessages(requestSessionId);
      if (this.sessionId === requestSessionId) {
        this.messages = requestMessages;
      }

      requestMessages.push({
        text: text,
        isUser: true,
      });

      if (requestMessages.length === 1) {
        const tempTitle = this.getLocalSessionTitle(requestSessionId, requestMessages);
        const existingSession = sessionStore.sessions.find((s) => s.session_id === requestSessionId);
        if (existingSession) {
          existingSession.title = existingSession.title || tempTitle;
          existingSession.message_count = requestMessages.length;
          existingSession.updated_at = new Date().toISOString();
          existingSession.isStreaming = true;
        } else {
          sessionStore.sessions.unshift({
            session_id: requestSessionId,
            title: tempTitle,
            message_count: requestMessages.length,
            updated_at: new Date().toISOString(),
            isStreaming: true,
          });
        }
      }

      this.userInput = '';
      this.isLoading = true;
      this.streamingSessionId = requestSessionId;

      requestMessages.push({
        text: '',
        isUser: false,
        isThinking: true,
        ragTrace: null,
        ragSteps: [],
        _groupedSteps: [],
      });
      const botMsgIdx = requestMessages.length - 1;
      this.mergeCachedSessionsIntoHistory();

      this.abortController = new AbortController();

      try {
        const response = await fetch('/chat/stream', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${authStore.token}`,
          },
          body: JSON.stringify({
            message: text,
            session_id: requestSessionId,
          }),
          signal: this.abortController.signal,
        });

        if (!response.ok) {
          if (response.status === 401) {
            authStore.handleLogout();
            throw new Error('登录已过期，请重新登录');
          }
          throw new Error(`HTTP ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) throw new Error('无法读取响应流');

        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          let eventEndIndex;
          while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
            const eventStr = buffer.slice(0, eventEndIndex);
            buffer = buffer.slice(eventEndIndex + 2);

            if (eventStr.startsWith('data: ')) {
              const dataStr = eventStr.slice(6);
              if (dataStr === '[DONE]') continue;
              try {
                const data = JSON.parse(dataStr);
                if (data.type === 'content') {
                  const botMsg = requestMessages[botMsgIdx];
                  if (!botMsg) continue;
                  if (botMsg.isThinking) {
                    botMsg.isThinking = false;
                  }
                  botMsg.text += data.content;
                } else if (data.type === 'trace') {
                  const botMsg = requestMessages[botMsgIdx];
                  if (botMsg) {
                    botMsg.ragTrace = data.rag_trace;
                  }
                } else if (data.type === 'rag_step') {
                  const msg = requestMessages[botMsgIdx];
                  if (!msg) continue;
                  if (!msg.ragSteps) msg.ragSteps = [];
                  msg.ragSteps.push(data.step);
                  msg._groupedSteps = this.appendRagStepToGroups(msg._groupedSteps || [], data.step);
                } else if (data.type === 'session_title') {
                  const s = sessionStore.sessions.find(
                    (item) => item.session_id === data.session_id
                  );
                  if (s) {
                    s.title = data.title;
                    s.updated_at = new Date().toISOString();
                    s.message_count = requestMessages.length;
                    s.isStreaming = data.session_id === this.streamingSessionId;
                  } else {
                    sessionStore.sessions.unshift({
                      session_id: data.session_id,
                      title: data.title,
                      message_count: requestMessages.length,
                      updated_at: new Date().toISOString(),
                      isStreaming: data.session_id === this.streamingSessionId,
                    });
                  }
                } else if (data.type === 'error') {
                  const botMsg = requestMessages[botMsgIdx];
                  if (!botMsg) continue;
                  botMsg.isThinking = false;
                  botMsg.text += `\n[Error: ${data.content}]`;
                }
              } catch (e) {
                console.warn('SSE parse error:', e);
              }
            }
          }
        }
      } catch (error: any) {
        const botMsg = requestMessages[botMsgIdx];
        if (!botMsg) return;
        if (error.name === 'AbortError') {
          botMsg.isThinking = false;
          if (!botMsg.text) {
            botMsg.text = '(已终止回答)';
          } else {
            botMsg.text += '\n\n_(回答已被终止)_';
          }
        } else {
          botMsg.isThinking = false;
          botMsg.text = `喵呜... 出了点问题：${error.message}`;
        }
      } finally {
        this.isLoading = false;
        this.streamingSessionId = null;
        this.abortController = null;
        this.mergeCachedSessionsIntoHistory();
      }
    },
  },
});
