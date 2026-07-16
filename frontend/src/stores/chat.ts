import { defineStore } from 'pinia';
import { watch, type WatchStopHandle } from 'vue';
import { useAuthStore } from './auth';
import { useSessionStore } from './sessions';
import { useRunsStore } from './runs';
import type { RunEventState } from '@/events/runEventReducer';
import api, { getPublicError, type PublicRequestError } from '@/utils/api';
import type { Message, RagStep, GroupedRagStep, HitlRequest, RagTrace } from '@/types/chat';

type ServerMessage = {
  id?: number;
  run_id?: string | null;
  sequence?: number;
  status?: string;
  type: string;
  content: string;
  timestamp?: string;
  rag_trace?: RagTrace | null;
};

const BUSY_RUN_STATUSES = new Set(['creating', 'queued', 'pending', 'running', 'cancelling']);
const RECOVERABLE_MESSAGE_STATUSES = new Set(['queued', 'streaming', 'waiting_input']);
const projectionStops = new WeakMap<object, Map<string, WatchStopHandle>>();

function projectionMap(store: object): Map<string, WatchStopHandle> {
  let stops = projectionStops.get(store);
  if (!stops) {
    stops = new Map();
    projectionStops.set(store, stops);
  }
  return stops;
}

function stopAllProjections(store: object) {
  const stops = projectionMap(store);
  stops.forEach((stop) => stop());
  stops.clear();
}

function appendPublicError(text: string, error: PublicRequestError): string {
  const rendered = `[${error.code}] ${error.message}`;
  return text.trim() ? `${text}\n\n${rendered}` : rendered;
}

function isHitlTrace(trace?: RagTrace | null): boolean {
  if (!trace) return false;
  return (
    trace.retrieval_status === 'needs_clarification' ||
    trace.retrieval_status === 'needs_scope_selection' ||
    trace.route === 'clarify' ||
    trace.route === 'scope_select'
  );
}

function normalizeLegacyHitl(message: Message): HitlRequest | null {
  if (message.isUser || !isHitlTrace(message.ragTrace)) return null;
  return {
    runId: message.runId,
    prompt: message.hitlPrompt || message.ragTrace?.hitl_prompt || message.text,
    options: message.hitlOptions || message.ragTrace?.hitl_options || [],
    route: message.ragTrace?.route,
    retrieval_status: message.ragTrace?.retrieval_status,
  };
}

function formatHitlText(hitl: HitlRequest): string {
  const options = hitl.options || [];
  if (!options.length) return hitl.prompt;
  return `${hitl.prompt}\n\n可选方向：\n${options.map((item) => `- ${item}`).join('\n')}`;
}

function createSessionId(messagesBySession: Record<string, Message[]>): string {
  let nextId = `session_${Date.now()}`;
  while (messagesBySession[nextId]) {
    nextId = `session_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  }
  return nextId;
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [] as Message[],
    messagesBySession: {} as Record<string, Message[]>,
    userInput: '',
    activeNav: 'newChat' as 'newChat' | 'history' | 'settings',
    sessionId: `session_${Date.now()}`,
  }),

  getters: {
    currentRunStatus(state): string | null {
      return useRunsStore().activeForThread(state.sessionId)?.status || null;
    },

    currentTransportStatus(state): string | null {
      return useRunsStore().activeForThread(state.sessionId)?.transportStatus || null;
    },

    isLoading(state): boolean {
      const run = useRunsStore().activeForThread(state.sessionId);
      return Boolean(run && BUSY_RUN_STATUSES.has(run.status));
    },

    isViewingStreamingSession(state): boolean {
      const run = useRunsStore().activeForThread(state.sessionId);
      return Boolean(run && BUSY_RUN_STATUSES.has(run.status));
    },

    isInputLocked(state): boolean {
      const run = useRunsStore().activeForThread(state.sessionId);
      return Boolean(run && BUSY_RUN_STATUSES.has(run.status));
    },

    currentPendingHitl(state): HitlRequest | null {
      const run = useRunsStore().activeForThread(state.sessionId);
      if (run?.pendingHitl) {
        return {
          runId: run.runId,
          hitlToken: run.pendingHitl.hitlToken || undefined,
          checkpointId: run.pendingHitl.checkpointId || undefined,
          prompt: run.pendingHitl.prompt,
          options: run.pendingHitl.options,
          route: run.pendingHitl.route || undefined,
          retrieval_status: run.pendingHitl.retrievalStatus || undefined,
          original_question: run.pendingHitl.originalQuestion || undefined,
        };
      }
      const messages = state.messagesBySession[state.sessionId] || [];
      const lastMessage = messages[messages.length - 1];
      return lastMessage ? normalizeLegacyHitl(lastMessage) : null;
    },

    inputPlaceholder(): string {
      if (this.currentPendingHitl) {
        return '输入自定义补充，或选择上方选项后发送...';
      }
      return '和喵喵说点什么吧... (Shift+Enter 换行)';
    },
  },

  actions: {
    resetWorkspace() {
      stopAllProjections(this);
      const runsStore = useRunsStore();
      runsStore.disconnectAll();
      runsStore.$reset();
      this.$reset();
    },

    ensureSessionMessages(sessionId: string): Message[] {
      if (!this.messagesBySession[sessionId]) {
        this.messagesBySession[sessionId] = [];
      }
      return this.messagesBySession[sessionId];
    },

    selectHitlOption(option: string) {
      this.userInput = option;
    },

    setViewedSession(sessionId: string, messages?: Message[]) {
      if (messages) this.messagesBySession[sessionId] = messages;
      this.sessionId = sessionId;
      this.messages = this.ensureSessionMessages(sessionId);
      this.activeNav = 'newChat';
    },

    getLocalSessionTitle(sessionId: string, messages: Message[]): string {
      const firstUserMessage = messages.find((message) => message.isUser && message.text.trim());
      if (!firstUserMessage) return sessionId;
      const title = firstUserMessage.text.trim();
      return title.length > 10 ? `${title.substring(0, 10)}...` : title;
    },

    mapServerMessages(messages: ServerMessage[]): Message[] {
      let awaitingLegacyHitlAnswer = false;
      let legacyResumeText: string | undefined;

      return (messages || []).map((message) => {
        const ragTrace = message.rag_trace || null;
        const isUser = message.type === 'human';
        const isHitlRequest = !message.run_id && !isUser && isHitlTrace(ragTrace);
        const isHitlAnswer = !message.run_id && isUser && awaitingLegacyHitlAnswer;
        const resumeTextForMessage =
          !message.run_id && !isUser && !isHitlRequest ? legacyResumeText : undefined;

        if (isHitlRequest) {
          awaitingLegacyHitlAnswer = true;
          legacyResumeText = undefined;
        } else if (isHitlAnswer) {
          awaitingLegacyHitlAnswer = false;
          legacyResumeText = message.content;
        } else if (!isUser) {
          legacyResumeText = undefined;
        }

        return {
          id: message.id,
          runId: message.run_id || undefined,
          sequence: message.sequence,
          status: message.status,
          text: message.content,
          isUser,
          isThinking: !isUser && ['queued', 'streaming'].includes(String(message.status || '')),
          isHitlRequest,
          isHitlAnswer,
          hitlPrompt: isHitlRequest ? ragTrace?.hitl_prompt || message.content : undefined,
          hitlOptions: isHitlRequest ? ragTrace?.hitl_options || [] : undefined,
          hitlResumeText: resumeTextForMessage,
          ragTrace,
        };
      });
    },

    mergeCachedSessionsIntoHistory() {
      const sessionStore = useSessionStore();
      const runsStore = useRunsStore();
      const sessions = sessionStore.sessions.map((session) => {
        const run = runsStore.activeForThread(session.session_id);
        const creating = Boolean(runsStore.pendingCreates[session.session_id]);
        return {
          ...session,
          status: creating ? 'creating' : run?.status || session.status,
          isStreaming: creating || Boolean(run && BUSY_RUN_STATUSES.has(run.status)),
        };
      });

      Object.entries(this.messagesBySession).forEach(([sessionId, messages]) => {
        if (!messages.length) return;
        const existingIndex = sessions.findIndex((session) => session.session_id === sessionId);
        const existing = existingIndex >= 0 ? sessions[existingIndex] : null;
        const run = runsStore.activeForThread(sessionId);
        const creating = Boolean(runsStore.pendingCreates[sessionId]);
        const localTitle = this.getLocalSessionTitle(sessionId, messages);
        const existingTitle = existing?.title;
        const title = !existingTitle || existingTitle === sessionId ? localTitle : existingTitle;
        const localSession = {
          session_id: sessionId,
          title,
          message_count: Math.max(existing?.message_count || 0, messages.length),
          updated_at: existing?.updated_at || new Date().toISOString(),
          version: existing?.version,
          status: creating ? 'creating' : run?.status || existing?.status,
          isStreaming: creating || Boolean(run && BUSY_RUN_STATUSES.has(run.status)),
        };

        if (existingIndex >= 0) sessions[existingIndex] = { ...existing, ...localSession };
        else sessions.unshift(localSession);
      });

      sessionStore.sessions = sessions;
    },

    appendRagStepToGroups(prev: GroupedRagStep[], step: RagStep): GroupedRagStep[] {
      const groups = prev ? [...prev] : [];
      const group = step.group || null;
      const groupLabel = step.group_label || group;
      if (group) {
        const index = groups.findIndex((item) => item.group === group);
        if (index >= 0) {
          const existing = groups[index];
          groups[index] = {
            group: existing.group,
            label: existing.label || groupLabel,
            steps: [...existing.steps, step],
            collapsed: existing.collapsed,
          };
          return groups;
        }
        return [...groups, { group, label: groupLabel, steps: [step], collapsed: true }];
      }

      const last = groups[groups.length - 1];
      if (last?.group === null) {
        groups[groups.length - 1] = { ...last, steps: [...last.steps, step] };
        return groups;
      }
      return [...groups, { group: null, label: null, steps: [step], collapsed: false }];
    },

    groupRagSteps(steps: RagStep[]): GroupedRagStep[] {
      return (steps || []).reduce(
        (groups, step) => this.appendRagStepToGroups(groups, step),
        [] as GroupedRagStep[]
      );
    },

    toggleStepGroup(messageIndex: number, groupIndex: number) {
      const message = this.messages[messageIndex];
      const group = message?._groupedSteps?.[groupIndex];
      if (group) group.collapsed = !group.collapsed;
    },

    projectRunState(run: RunEventState) {
      const messages = this.ensureSessionMessages(run.threadId);
      const assistant = messages.find(
        (message) =>
          !message.isUser &&
          (message.runId === run.runId ||
            (run.assistantMessageId !== null && message.id === run.assistantMessageId))
      );
      const user = messages.find(
        (message) =>
          message.isUser &&
          (message.runId === run.runId ||
            (run.userMessageId !== null && message.id === run.userMessageId))
      );
      if (user) {
        user.runId = run.runId;
        if (run.userMessageId !== null) user.id = run.userMessageId;
      }
      if (!assistant) return;

      assistant.runId = run.runId;
      if (run.assistantMessageId !== null) assistant.id = run.assistantMessageId;
      assistant.status = run.messageStatus || run.status;
      assistant.isThinking = BUSY_RUN_STATUSES.has(run.status) && !run.messageText;
      assistant.isHitlRequest = Boolean(run.pendingHitl);
      assistant.hitlResumeText = run.lastResumeAnswer || assistant.hitlResumeText;

      if (run.pendingHitl) {
        const hitl: HitlRequest = {
          runId: run.runId,
          hitlToken: run.pendingHitl.hitlToken || undefined,
          checkpointId: run.pendingHitl.checkpointId || undefined,
          prompt: run.pendingHitl.prompt,
          options: run.pendingHitl.options,
          route: run.pendingHitl.route || undefined,
          retrieval_status: run.pendingHitl.retrievalStatus || undefined,
          original_question: run.pendingHitl.originalQuestion || undefined,
        };
        assistant.text = formatHitlText(hitl);
        assistant.isThinking = false;
        assistant.hitlPrompt = hitl.prompt;
        assistant.hitlOptions = hitl.options || [];
      } else {
        assistant.hitlPrompt = undefined;
        assistant.hitlOptions = undefined;
        if (run.messageText || run.messageStatus) assistant.text = run.messageText;
      }

      if (run.ragTrace) assistant.ragTrace = run.ragTrace as RagTrace;
      const steps = run.toolProgress.map((item) => item.step as unknown as RagStep);
      assistant.ragSteps = steps;
      assistant._groupedSteps = this.groupRagSteps(steps);

      if (run.terminal) {
        assistant.isThinking = false;
        if (!assistant.text && run.error) {
          assistant.text = appendPublicError('', getPublicError(run.error));
        }
      }
      if (this.sessionId === run.threadId) this.messages = messages;
      this.mergeCachedSessionsIntoHistory();
    },

    attachRunProjection(runId: string, threadId: string) {
      const stops = projectionMap(this);
      if (stops.has(runId)) return;
      const runsStore = useRunsStore();
      const stop = watch(
        () => runsStore.byId[runId],
        (run) => {
          if (run?.threadId === threadId) this.projectRunState(run);
        },
        { deep: true, immediate: true, flush: 'sync' }
      );
      stops.set(runId, stop);
    },

    appendRunConnectionError(runId: string, error: unknown) {
      const publicError = getPublicError(error);
      const run = useRunsStore().byId[runId];
      if (!run || run.terminal || run.status === 'waiting_input') return;
      const assistant = this.ensureSessionMessages(run.threadId).find(
        (message) => !message.isUser && message.runId === runId
      );
      if (!assistant) return;
      assistant.isThinking = false;
      assistant.status = 'failed';
      assistant.text = appendPublicError(assistant.text, publicError);
      if (publicError.code === 'AUTHENTICATION_REQUIRED') useAuthStore().handleLogout();
    },

    async connectRun(runId: string, token: string) {
      try {
        await useRunsStore().connect(runId, token);
      } catch (error) {
        this.appendRunConnectionError(runId, error);
      } finally {
        const run = useRunsStore().byId[runId];
        if (run) this.projectRunState(run);
      }
    },

    async restoreRunsForSession(sessionId: string) {
      const authStore = useAuthStore();
      const runsStore = useRunsStore();
      const runIds = Array.from(
        new Set(
          this.ensureSessionMessages(sessionId)
            .filter(
              (message) =>
                !message.isUser &&
                message.runId &&
                RECOVERABLE_MESSAGE_STATUSES.has(String(message.status || ''))
            )
            .map((message) => message.runId as string)
        )
      );

      for (const runId of runIds) {
        this.attachRunProjection(runId, sessionId);
        try {
          const run = await runsStore.replay(runId, authStore.token);
          this.projectRunState(run);
          if (!run.terminal && run.status !== 'waiting_input') {
            void this.connectRun(runId, authStore.token);
          }
        } catch (error) {
          this.appendRunConnectionError(runId, error);
        }
      }
    },

    async loadSession(sessionId: string) {
      const sessionStore = useSessionStore();
      const cachedMessages = this.messagesBySession[sessionId];
      this.setViewedSession(sessionId, cachedMessages || []);
      sessionStore.showHistorySidebar = false;

      try {
        const records: ServerMessage[] = [];
        let after = 0;
        for (let page = 0; page < 1000; page += 1) {
          const response = await api.get(`/sessions/${encodeURIComponent(sessionId)}`, {
            params: { after, limit: 200 },
          });
          records.push(...(response.data.messages || []));
          const nextCursor = response.data.next_cursor;
          if (!Number.isInteger(nextCursor) || nextCursor <= after) break;
          after = nextCursor;
        }
        const loadedMessages = this.mapServerMessages(records);
        this.setViewedSession(sessionId, loadedMessages);
        this.mergeCachedSessionsIntoHistory();
        await this.restoreRunsForSession(sessionId);
      } catch (error) {
        if (!cachedMessages && this.sessionId === sessionId) this.messages = [];
        if (cachedMessages) await this.restoreRunsForSession(sessionId);
        throw getPublicError(error);
      }
    },

    handleNewChat() {
      const sessionId = createSessionId(this.messagesBySession);
      this.messagesBySession[sessionId] = [];
      this.setViewedSession(sessionId);
      useSessionStore().showHistorySidebar = false;
    },

    handleClearChat() {
      if (useRunsStore().activeForThread(this.sessionId)) {
        alert('当前会话仍有活跃运行，请先终止或等待完成后再清空');
        return;
      }
      if (confirm('确定要清空当前对话吗？喵？')) {
        this.messagesBySession[this.sessionId] = [];
        this.messages = this.messagesBySession[this.sessionId];
      }
    },

    handleStop() {
      const runsStore = useRunsStore();
      const activeRun = runsStore.activeForThread(this.sessionId);
      if (!activeRun || activeRun.status === 'cancelling') return;
      void runsStore.cancel(activeRun.runId, useAuthStore().token).catch((error) => {
        alert(getPublicError(error).message);
      });
    },

    async resumeHitl(hitl: HitlRequest, answer: string) {
      if (!hitl.runId || !hitl.hitlToken) {
        alert('这条旧版人工介入记录无法原地恢复，请新建问题重试。');
        return;
      }
      const authStore = useAuthStore();
      const runsStore = useRunsStore();
      const run = runsStore.byId[hitl.runId];
      if (!run) {
        alert('运行状态尚未恢复，请刷新会话后重试。');
        return;
      }

      this.userInput = '';
      this.attachRunProjection(hitl.runId, run.threadId);
      const assistant = this.ensureSessionMessages(run.threadId).find(
        (message) => !message.isUser && message.runId === hitl.runId
      );
      if (assistant) {
        assistant.hitlResumeText = answer;
        assistant.isHitlRequest = false;
        assistant.isThinking = true;
        assistant.text = run.messageText;
      }

      try {
        await runsStore.resume(hitl.runId, {
          token: authStore.token,
          hitlToken: hitl.hitlToken,
          answer,
        });
      } catch (error) {
        this.userInput = answer;
        this.projectRunState(run);
        const publicError = getPublicError(error);
        if (publicError.code === 'AUTHENTICATION_REQUIRED') authStore.handleLogout();
        alert(publicError.message);
      } finally {
        this.projectRunState(runsStore.byId[hitl.runId] || run);
      }
    },

    async handleSend() {
      const authStore = useAuthStore();
      const sessionStore = useSessionStore();
      const runsStore = useRunsStore();
      if (!authStore.isAuthenticated) {
        alert('请先登录');
        return;
      }

      const text = this.userInput.trim();
      if (!text) return;
      const pendingHitl = this.currentPendingHitl;
      if (pendingHitl) {
        await this.resumeHitl(pendingHitl, text);
        return;
      }
      if (this.isLoading) {
        alert('当前会话已有回答正在生成，请先等待或终止该运行');
        return;
      }

      const threadId = this.sessionId;
      const messages = this.ensureSessionMessages(threadId);
      const userMessage: Message = {
        text,
        isUser: true,
        status: 'completed',
      };
      const assistantMessage: Message = {
        text: '',
        isUser: false,
        isThinking: true,
        thinkingStartedAt: Date.now(),
        status: 'creating',
        ragTrace: null,
        ragSteps: [],
        _groupedSteps: [],
      };
      messages.push(userMessage, assistantMessage);
      if (this.sessionId === threadId) this.messages = messages;
      this.userInput = '';
      this.mergeCachedSessionsIntoHistory();

      try {
        const session = sessionStore.sessions.find((item) => item.session_id === threadId);
        const createPromise = runsStore.create({
          threadId,
          message: text,
          token: authStore.token,
          expectedThreadVersion: session?.version,
          multitaskStrategy: 'reject',
          onDisconnect: 'continue',
          approvedTools: [],
        });
        this.mergeCachedSessionsIntoHistory();
        const created = await createPromise;
        const runId = created.response.run.id;
        userMessage.id = created.response.run.user_message_id;
        userMessage.runId = runId;
        assistantMessage.id = created.response.run.assistant_message_id;
        assistantMessage.runId = runId;
        assistantMessage.status = created.response.run.status;
        if (session) session.version = created.response.thread_version;
        this.attachRunProjection(runId, threadId);
        this.projectRunState(created.state);
        await this.connectRun(runId, authStore.token);
      } catch (error) {
        const publicError = getPublicError(error);
        assistantMessage.isThinking = false;
        assistantMessage.status = 'failed';
        assistantMessage.text = appendPublicError(assistantMessage.text, publicError);
        if (publicError.code === 'AUTHENTICATION_REQUIRED') authStore.handleLogout();
      } finally {
        this.mergeCachedSessionsIntoHistory();
      }
    },

    removeSessionState(sessionId: string) {
      const runsStore = useRunsStore();
      Object.values(runsStore.byId)
        .filter((run) => run.threadId === sessionId)
        .forEach((run) => {
          projectionMap(this).get(run.runId)?.();
          projectionMap(this).delete(run.runId);
          runsStore.remove(run.runId);
        });
      delete this.messagesBySession[sessionId];
    },
  },
});
