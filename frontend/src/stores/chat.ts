import { defineStore } from 'pinia';
import { watch, type WatchStopHandle } from 'vue';
import { useAuthStore } from './auth';
import { useSessionStore } from './sessions';
import { useRunsStore } from './runs';
import type { RunEventState } from '@/events/runEventReducer';
import { getThreadMessages } from '@/threads/threadClient';
import type { ThreadMessage } from '@/types/threads';
import { getPublicError, type PublicRequestError } from '@/utils/api';
import type { Message, RagStep, GroupedRagStep, HitlRequest, RagTrace } from '@/types/chat';

const BUSY_RUN_STATUSES = new Set(['creating', 'queued', 'pending', 'running', 'cancelling']);
const RECOVERABLE_MESSAGE_STATUSES = new Set(['queued', 'streaming', 'waiting_input']);
const projectionStops = new WeakMap<object, Map<string, WatchStopHandle>>();

interface ThreadMessagePageState {
  previousCursor: number | null;
  hasOlder: boolean;
  loadingOlder: boolean;
}

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

function messageIdentity(message: Message): string {
  if (message.id !== undefined) return `id:${message.id}`;
  if (message.sequence !== undefined) return `sequence:${message.sequence}`;
  return `local:${message.runId || ''}:${message.isUser ? 'user' : 'assistant'}:${message.text}`;
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [] as Message[],
    messagesBySession: {} as Record<string, Message[]>,
    messagePagesBySession: {} as Record<string, ThreadMessagePageState>,
    userInput: '',
    activeNav: 'newChat' as 'newChat' | 'history' | 'settings',
    sessionId: '',
    isCreatingThread: false,
  }),

  getters: {
    currentRunStatus(state): string | null {
      return (
        useRunsStore().activeForThread(state.sessionId)?.status ||
        useSessionStore().threadById(state.sessionId)?.activeRunStatus ||
        null
      );
    },

    currentTransportStatus(state): string | null {
      return useRunsStore().activeForThread(state.sessionId)?.transportStatus || null;
    },

    isLoading(state): boolean {
      const run = useRunsStore().activeForThread(state.sessionId);
      const status = run?.status || useSessionStore().threadById(state.sessionId)?.activeRunStatus;
      return state.isCreatingThread || BUSY_RUN_STATUSES.has(String(status || ''));
    },

    isViewingStreamingSession(state): boolean {
      const run = useRunsStore().activeForThread(state.sessionId);
      return Boolean(run && BUSY_RUN_STATUSES.has(run.status));
    },

    isInputLocked(state): boolean {
      const run = useRunsStore().activeForThread(state.sessionId);
      if (state.isCreatingThread) return true;
      if (run) return BUSY_RUN_STATUSES.has(run.status);
      return Boolean(useSessionStore().threadById(state.sessionId)?.activeRunStatus);
    },

    hasOlderMessages(state): boolean {
      return Boolean(state.messagePagesBySession[state.sessionId]?.hasOlder);
    },

    isLoadingOlderMessages(state): boolean {
      return Boolean(state.messagePagesBySession[state.sessionId]?.loadingOlder);
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

    ensureMessagePage(sessionId: string): ThreadMessagePageState {
      if (!this.messagePagesBySession[sessionId]) {
        this.messagePagesBySession[sessionId] = {
          previousCursor: null,
          hasOlder: false,
          loadingOlder: false,
        };
      }
      return this.messagePagesBySession[sessionId];
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

    mapServerMessages(messages: ThreadMessage[]): Message[] {
      let awaitingLegacyHitlAnswer = false;
      let legacyResumeText: string | undefined;

      return (messages || []).map((message) => {
        const ragTrace = message.rag_trace || null;
        const isUser = message.role === 'user';
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
      sessionStore.sessions.forEach((session) => {
        const run = runsStore.activeForThread(session.thread_id);
        const creating = Boolean(runsStore.pendingCreates[session.thread_id]);
        if (creating) sessionStore.setRunView(session.thread_id, null, 'creating');
        else if (run) sessionStore.setRunView(session.thread_id, run.runId, run.status);
      });

      Object.entries(this.messagesBySession).forEach(([sessionId, messages]) => {
        const existing = sessionStore.threadById(sessionId);
        if (!existing || !messages.length) return;
        const localTitle = this.getLocalSessionTitle(sessionId, messages);
        if (!existing.title || existing.title === sessionId || existing.title === '新对话') {
          existing.title = localTitle;
        }
        existing.message_count = Math.max(existing.message_count, messages.length);
      });
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
      const sessionStore = useSessionStore();
      sessionStore.setRunView(
        run.threadId,
        run.terminal ? null : run.runId,
        run.terminal ? null : run.status
      );
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
        const response = await getThreadMessages(sessionId, { limit: 200 });
        const loadedMessages = this.mapServerMessages(response.messages || []);
        this.messagePagesBySession[sessionId] = {
          previousCursor: response.previous_cursor,
          hasOlder: response.previous_cursor !== null,
          loadingOlder: false,
        };
        this.setViewedSession(sessionId, loadedMessages);
        this.mergeCachedSessionsIntoHistory();
        await this.restoreRunsForSession(sessionId);
      } catch (error) {
        if (!cachedMessages && this.sessionId === sessionId) this.messages = [];
        if (cachedMessages) await this.restoreRunsForSession(sessionId);
        throw getPublicError(error);
      }
    },

    async loadOlderMessages() {
      const sessionId = this.sessionId;
      if (!sessionId) return;
      const page = this.ensureMessagePage(sessionId);
      if (!page.hasOlder || page.loadingOlder || page.previousCursor === null) return;

      page.loadingOlder = true;
      try {
        const response = await getThreadMessages(sessionId, {
          before: page.previousCursor,
          limit: 200,
        });
        const current = this.ensureSessionMessages(sessionId);
        const seen = new Set(current.map(messageIdentity));
        const older = this.mapServerMessages(response.messages || []).filter(
          (message) => !seen.has(messageIdentity(message))
        );
        const merged = [...older, ...current].sort((left, right) => {
          if (left.sequence === undefined) return 1;
          if (right.sequence === undefined) return -1;
          return left.sequence - right.sequence;
        });
        this.messagesBySession[sessionId] = merged;
        if (this.sessionId === sessionId) this.messages = merged;
        page.previousCursor = response.previous_cursor;
        page.hasOlder = response.previous_cursor !== null;
      } catch (error) {
        throw getPublicError(error);
      } finally {
        page.loadingOlder = false;
      }
    },

    async createNewThread(title?: string) {
      if (this.isCreatingThread) return null;
      const selectedBeforeCreate = this.sessionId;
      this.isCreatingThread = true;
      try {
        const thread = await useSessionStore().createSession(title);
        this.messagesBySession[thread.thread_id] = [];
        this.messagePagesBySession[thread.thread_id] = {
          previousCursor: null,
          hasOlder: false,
          loadingOlder: false,
        };
        if (this.sessionId === selectedBeforeCreate) {
          this.setViewedSession(thread.thread_id);
        }
        return thread;
      } finally {
        this.isCreatingThread = false;
      }
    },

    async handleNewChat() {
      this.userInput = '';
      this.setViewedSession('', []);
      useSessionStore().showHistorySidebar = false;
      try {
        await this.createNewThread();
      } catch (error) {
        alert(getPublicError(error).message);
      }
    },

    async handleClearChat() {
      const threadId = this.sessionId;
      if (!threadId) {
        await this.handleNewChat();
        return;
      }
      if (
        useRunsStore().activeForThread(threadId) ||
        useSessionStore().threadById(threadId)?.isStreaming
      ) {
        alert('当前会话仍有活跃运行，请先终止或等待完成后再清空');
        return;
      }
      if (!confirm('确定要永久删除当前对话吗？喵？')) return;

      try {
        await useSessionStore().deleteSession(threadId);
        this.removeSessionState(threadId);
        this.setViewedSession('', []);
        await this.createNewThread();
      } catch (error) {
        alert(getPublicError(error).message);
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

      let threadId = this.sessionId;
      if (!threadId) {
        try {
          const thread = await this.createNewThread(text.replace(/\s+/g, ' ').slice(0, 32));
          if (!thread) return;
          threadId = thread.thread_id;
        } catch (error) {
          const publicError = getPublicError(error);
          alert(publicError.message);
          return;
        }
      }
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
        const session = sessionStore.sessions.find((item) => item.thread_id === threadId);
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
      delete this.messagePagesBySession[sessionId];
    },
  },
});
