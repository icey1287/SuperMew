import { defineStore } from 'pinia';
import { watch, type WatchStopHandle } from 'vue';
import { useAuthStore } from './auth';
import { useThreadStore } from './threads';
import { useRunsStore } from './runs';
import { useCapabilityStore } from './capabilities';
import type { RunEventState } from '@/events/runEventReducer';
import { getThreadMessages } from '@/threads/threadClient';
import type { ThreadMessage } from '@/types/threads';
import { getPublicError, type PublicRequestError } from '@/utils/api';
import type { Message, RagStep, GroupedRagStep, HitlRequest, RagTrace } from '@/types/chat';
import type { CapabilityExecutionMessage } from '@/types/capabilities';

const BUSY_RUN_STATUSES = new Set(['creating', 'queued', 'pending', 'running', 'cancelling']);
const RECOVERABLE_MESSAGE_STATUSES = new Set(['queued', 'streaming', 'waiting_input']);
const TERMINAL_MESSAGE_STATUSES = new Set(['completed', 'failed', 'cancelled', 'incomplete']);
const projectionStops = new WeakMap<object, Map<string, WatchStopHandle>>();

interface ThreadMessagePageState {
  previousCursor: number | null;
  hasOlder: boolean;
  loadingOlder: boolean;
}

interface SendOptions {
  approvalConfirmed?: boolean;
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

function publicMessageContent(message: ThreadMessage): string {
  const skillName = message.skill_name?.trim();
  if (!skillName || message.role !== 'user') return message.content;
  const activation = `/${skillName}`;
  if (!message.content.startsWith(activation)) return message.content;
  const remainder = message.content.slice(activation.length);
  if (remainder && !/^\s/.test(remainder)) return message.content;
  const body = remainder.trimStart();
  if (skillName !== 'sandbox') return body;
  const jsonStart = body.indexOf('{');
  if (jsonStart < 0) return body;
  try {
    const payload = JSON.parse(body.slice(jsonStart));
    return typeof payload?.source === 'string' ? payload.source : body;
  } catch {
    return body;
  }
}

function messageIdentity(message: Message): string {
  if (message.id !== undefined) return `id:${message.id}`;
  if (message.sequence !== undefined) return `sequence:${message.sequence}`;
  return `local:${message.runId || ''}:${message.isUser ? 'user' : 'assistant'}:${message.text}`;
}

function isTerminalMessage(message: Message | undefined): boolean {
  return Boolean(message && TERMINAL_MESSAGE_STATUSES.has(String(message.status || '')));
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [] as Message[],
    messagesByThread: {} as Record<string, Message[]>,
    messagePagesByThread: {} as Record<string, ThreadMessagePageState>,
    userInput: '',
    activeNav: 'newChat' as
      'newChat' | 'history' | 'settings' | 'models' | 'evaluations' | 'capabilities-admin',
    threadId: '',
    isCreatingThread: false,
    loadingThreadId: '',
    threadLoadError: '',
  }),

  getters: {
    currentRunStatus(state): string | null {
      return (
        useRunsStore().activeForThread(state.threadId)?.status ||
        useThreadStore().threadById(state.threadId)?.activeRunStatus ||
        null
      );
    },

    currentTransportStatus(state): string | null {
      return useRunsStore().activeForThread(state.threadId)?.transportStatus || null;
    },

    currentTransportError(state) {
      return useRunsStore().activeForThread(state.threadId)?.transportError || null;
    },

    isResumingHitl(state): boolean {
      const run = useRunsStore().activeForThread(state.threadId);
      return useRunsStore().isResumeInFlight(run?.runId);
    },

    isLoading(state): boolean {
      const run = useRunsStore().activeForThread(state.threadId);
      const status = run?.status || useThreadStore().threadById(state.threadId)?.activeRunStatus;
      return (
        state.isCreatingThread ||
        Boolean(state.loadingThreadId && state.loadingThreadId === state.threadId) ||
        this.isResumingHitl ||
        BUSY_RUN_STATUSES.has(String(status || ''))
      );
    },

    isViewingStreamingThread(state): boolean {
      const run = useRunsStore().activeForThread(state.threadId);
      return Boolean(run && BUSY_RUN_STATUSES.has(run.status));
    },

    isInputLocked(state): boolean {
      const run = useRunsStore().activeForThread(state.threadId);
      if (
        state.isCreatingThread ||
        Boolean(state.loadingThreadId && state.loadingThreadId === state.threadId)
      ) {
        return true;
      }
      if (useRunsStore().isResumeInFlight(run?.runId)) return true;
      if (run) return BUSY_RUN_STATUSES.has(run.status);
      return Boolean(useThreadStore().threadById(state.threadId)?.activeRunStatus);
    },

    hasOlderMessages(state): boolean {
      return Boolean(state.messagePagesByThread[state.threadId]?.hasOlder);
    },

    isLoadingOlderMessages(state): boolean {
      return Boolean(state.messagePagesByThread[state.threadId]?.loadingOlder);
    },

    currentPendingHitl(state): HitlRequest | null {
      const run = useRunsStore().activeForThread(state.threadId);
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
      return null;
    },

    inputPlaceholder(): string {
      if (this.currentPendingHitl) {
        return '输入自定义补充，或选择上方选项后发送...';
      }
      const skillName = useCapabilityStore().selectedSkillName;
      if (skillName === 'web-research') return '描述要调研的公开问题、时间范围或来源偏好…';
      if (skillName === 'sql-assistant') return '描述要分析的业务问题、指标、维度与时间范围…';
      if (skillName === 'sandbox') return '输入要在隔离 Sandbox 中执行的源码…';
      if (skillName === 'knowledge-base') return '询问已上传文档，并说明需要引用的范围…';
      return '和喵喵说点什么吧... (Shift+Enter 换行)';
    },
  },

  actions: {
    resetWorkspace() {
      stopAllProjections(this);
      const runsStore = useRunsStore();
      useCapabilityStore().reset();
      runsStore.disconnectAll();
      runsStore.$reset();
      this.$reset();
    },

    ensureThreadMessages(threadId: string): Message[] {
      if (!this.messagesByThread[threadId]) {
        this.messagesByThread[threadId] = [];
      }
      return this.messagesByThread[threadId];
    },

    ensureMessagePage(threadId: string): ThreadMessagePageState {
      if (!this.messagePagesByThread[threadId]) {
        this.messagePagesByThread[threadId] = {
          previousCursor: null,
          hasOlder: false,
          loadingOlder: false,
        };
      }
      return this.messagePagesByThread[threadId];
    },

    selectHitlOption(option: string) {
      this.userInput = option;
    },

    setViewedThread(threadId: string, messages?: Message[]) {
      if (messages) this.messagesByThread[threadId] = messages;
      this.threadId = threadId;
      this.messages = this.ensureThreadMessages(threadId);
      this.activeNav = 'newChat';
      useCapabilityStore().setActiveThread(threadId || null);
    },

    getLocalThreadTitle(threadId: string, messages: Message[]): string {
      const firstUserMessage = messages.find((message) => message.isUser && message.text.trim());
      if (!firstUserMessage) return threadId;
      const title = firstUserMessage.text.trim();
      return title.length > 10 ? `${title.substring(0, 10)}...` : title;
    },

    mapServerMessages(messages: ThreadMessage[]): Message[] {
      return (messages || []).map((message) => {
        const ragTrace = message.rag_trace || null;
        const isUser = message.role === 'user';

        return {
          id: message.id,
          runId: message.run_id || undefined,
          sequence: message.sequence,
          status: message.status,
          text: publicMessageContent(message),
          isUser,
          isThinking: !isUser && ['queued', 'streaming'].includes(String(message.status || '')),
          isHitlRequest: false,
          isHitlAnswer: false,
          ragTrace,
          skillName: message.skill_name || null,
        };
      });
    },

    mergeCachedThreadsIntoHistory() {
      const threadStore = useThreadStore();
      const runsStore = useRunsStore();
      threadStore.threads.forEach((thread) => {
        const run = runsStore.activeForThread(thread.thread_id);
        const creating = Boolean(runsStore.pendingCreates[thread.thread_id]);
        if (creating) threadStore.setRunView(thread.thread_id, null, 'creating');
        else if (run) threadStore.setRunView(thread.thread_id, run.runId, run.status);
      });

      Object.entries(this.messagesByThread).forEach(([threadId, messages]) => {
        const existing = threadStore.threadById(threadId);
        if (!existing || !messages.length) return;
        const localTitle = this.getLocalThreadTitle(threadId, messages);
        if (!existing.title || existing.title === threadId || existing.title === '新对话') {
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
      const messages = this.ensureThreadMessages(run.threadId);
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
      const preservePersistedTerminal =
        isTerminalMessage(assistant) &&
        !TERMINAL_MESSAGE_STATUSES.has(String(run.messageStatus || ''));
      if (!preservePersistedTerminal || run.terminal) {
        useThreadStore().setRunView(
          run.threadId,
          run.terminal ? null : run.runId,
          run.terminal ? null : run.status
        );
      }
      if (user) {
        user.runId = run.runId;
        if (run.userMessageId !== null) user.id = run.userMessageId;
      }
      if (!assistant) return;

      assistant.runId = run.runId;
      if (run.assistantMessageId !== null) assistant.id = run.assistantMessageId;
      if (!preservePersistedTerminal) {
        assistant.status = run.messageStatus || run.status;
        assistant.isThinking = BUSY_RUN_STATUSES.has(run.status) && !run.messageText;
        assistant.isHitlRequest = Boolean(run.pendingHitl);
        assistant.hitlResumeText = run.lastResumeAnswer || assistant.hitlResumeText;
        assistant.runActiveDurationMs = run.activeDurationMs;
        assistant.runActiveStartedAt = run.activeStartedAt;

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
          assistant.text = '';
          assistant.isThinking = false;
          assistant.hitlPrompt = hitl.prompt;
          assistant.hitlOptions = hitl.options || [];
        } else {
          assistant.hitlPrompt = undefined;
          assistant.hitlOptions = undefined;
          if (run.messageText || run.messageStatus) assistant.text = run.messageText;
        }
      }

      if (run.ragTrace) assistant.ragTrace = run.ragTrace as RagTrace;
      const steps = run.toolProgress.map((item) => item.step as unknown as RagStep);
      assistant.ragSteps = steps;
      assistant.runTimeline = run.timeline;
      assistant.artifacts = run.artifacts;
      assistant.skillName = run.activeSkillName || assistant.skillName;
      assistant.skillVersion = run.activeSkillVersion || assistant.skillVersion;
      assistant._groupedSteps = this.groupRagSteps(steps);

      if (run.terminal && !preservePersistedTerminal) {
        assistant.isThinking = false;
        if (!assistant.text && run.error) {
          assistant.text = appendPublicError('', getPublicError(run.error));
        }
      }
      if (this.threadId === run.threadId) this.messages = messages;
      this.mergeCachedThreadsIntoHistory();
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

    async connectRun(runId: string, token: string) {
      try {
        await useRunsStore().connect(runId, token);
      } catch {
        // Transport state is projected separately; only terminal Run Events may
        // change the authoritative Message lifecycle.
      } finally {
        const run = useRunsStore().byId[runId];
        if (run) this.projectRunState(run);
      }
    },

    async reconnectCurrentRun() {
      const run = useRunsStore().activeForThread(this.threadId);
      if (!run || run.terminal || run.status === 'waiting_input') return;
      await this.connectRun(run.runId, useAuthStore().token);
    },

    async restoreRunProjection(runId: string, threadId?: string) {
      const authStore = useAuthStore();
      const runsStore = useRunsStore();
      const effectiveThreadId = threadId || this.threadId;
      this.attachRunProjection(runId, effectiveThreadId);
      const run = await runsStore.replay(runId, authStore.token);
      this.projectRunState(run);
      if (!run.terminal && run.status !== 'waiting_input') {
        void this.connectRun(runId, authStore.token);
      }
      return run;
    },

    async restoreRunsForThread(threadId: string) {
      const runsStore = useRunsStore();
      const assistantMessages = [...this.ensureThreadMessages(threadId)]
        .reverse()
        .filter((message) => !message.isUser && message.runId);
      const latestRunId = assistantMessages[0]?.runId;
      const runIds = Array.from(
        new Set(
          assistantMessages
            .filter(
              (message) =>
                message.runId === latestRunId ||
                RECOVERABLE_MESSAGE_STATUSES.has(String(message.status || ''))
            )
            .map((message) => message.runId as string)
        )
      );

      const restore = async (runId: string) => {
        try {
          await this.restoreRunProjection(runId, threadId);
        } catch (error) {
          const assistant = this.ensureThreadMessages(threadId).find(
            (message) => !message.isUser && message.runId === runId
          );
          const run =
            runsStore.byId[runId] ||
            (isTerminalMessage(assistant) ? null : runsStore.ensure(runId, threadId));
          if (!run) return;
          runsStore.setTransport(runId, 'closed', 0, error);
          this.projectRunState(run);
        }
      };

      for (let index = 0; index < runIds.length; index += 4) {
        await Promise.all(runIds.slice(index, index + 4).map(restore));
      }
    },

    async loadThread(threadId: string) {
      const threadStore = useThreadStore();
      const cachedMessages = this.messagesByThread[threadId];
      this.setViewedThread(threadId, cachedMessages || []);
      threadStore.showHistorySidebar = false;
      this.loadingThreadId = threadId;
      this.threadLoadError = '';

      try {
        const response = await getThreadMessages(threadId, { limit: 200 });
        const loadedMessages = this.mapServerMessages(response.messages || []);
        this.messagePagesByThread[threadId] = {
          previousCursor: response.previous_cursor,
          hasOlder: response.previous_cursor !== null,
          loadingOlder: false,
        };
        const messagesNewestFirst = [...loadedMessages].reverse();
        const latestRunMessage =
          messagesNewestFirst.find((message) => !message.isUser && message.runId) ||
          messagesNewestFirst.find((message) => message.runId);
        useCapabilityStore().restoreThreadSkill(latestRunMessage?.skillName ?? null, threadId);
        this.setViewedThread(threadId, loadedMessages);
        this.mergeCachedThreadsIntoHistory();
        await this.restoreRunsForThread(threadId);
      } catch (error) {
        const publicError = getPublicError(error);
        this.threadLoadError = publicError.message;
        if (!cachedMessages && this.threadId === threadId) this.messages = [];
        if (cachedMessages) await this.restoreRunsForThread(threadId);
        throw publicError;
      } finally {
        if (this.loadingThreadId === threadId) this.loadingThreadId = '';
      }
    },

    async loadOlderMessages() {
      const threadId = this.threadId;
      if (!threadId) return;
      const page = this.ensureMessagePage(threadId);
      if (!page.hasOlder || page.loadingOlder || page.previousCursor === null) return;

      page.loadingOlder = true;
      try {
        const response = await getThreadMessages(threadId, {
          before: page.previousCursor,
          limit: 200,
        });
        const current = this.ensureThreadMessages(threadId);
        const seen = new Set(current.map(messageIdentity));
        const older = this.mapServerMessages(response.messages || []).filter(
          (message) => !seen.has(messageIdentity(message))
        );
        const merged = [...older, ...current].sort((left, right) => {
          if (left.sequence === undefined) return 1;
          if (right.sequence === undefined) return -1;
          return left.sequence - right.sequence;
        });
        this.messagesByThread[threadId] = merged;
        if (this.threadId === threadId) this.messages = merged;
        page.previousCursor = response.previous_cursor;
        page.hasOlder = response.previous_cursor !== null;
      } catch (error) {
        throw getPublicError(error);
      } finally {
        page.loadingOlder = false;
      }
    },

    async createNewThread(title?: string, initialSkillName?: string | null) {
      if (this.isCreatingThread) return null;
      const selectedBeforeCreate = this.threadId;
      this.isCreatingThread = true;
      try {
        const thread = await useThreadStore().createThread(title);
        this.messagesByThread[thread.thread_id] = [];
        this.messagePagesByThread[thread.thread_id] = {
          previousCursor: null,
          hasOlder: false,
          loadingOlder: false,
        };
        if (this.threadId === selectedBeforeCreate) {
          if (initialSkillName !== undefined) {
            useCapabilityStore().selectSkill(initialSkillName, thread.thread_id);
          }
          this.setViewedThread(thread.thread_id);
        }
        return thread;
      } finally {
        this.isCreatingThread = false;
      }
    },

    handleNewChat() {
      this.userInput = '';
      this.threadId = '';
      this.messages = [];
      this.activeNav = 'newChat';
      delete this.messagesByThread[''];
      delete this.messagePagesByThread[''];
      useCapabilityStore().setActiveThread(null);
      useThreadStore().showHistorySidebar = false;
    },

    async handleClearChat() {
      const threadId = this.threadId;
      if (!threadId) {
        await this.handleNewChat();
        return;
      }
      if (
        useRunsStore().activeForThread(threadId) ||
        useThreadStore().threadById(threadId)?.isStreaming
      ) {
        alert('当前会话仍有活跃运行，请先终止或等待完成后再清空');
        return;
      }
      if (!confirm('确定要永久删除当前对话吗？喵？')) return;

      try {
        await useThreadStore().deleteThread(threadId);
        this.removeThreadState(threadId);
        this.handleNewChat();
      } catch (error) {
        alert(getPublicError(error).message);
      }
    },

    handleStop() {
      const runsStore = useRunsStore();
      const activeRun = runsStore.activeForThread(this.threadId);
      if (!activeRun || activeRun.status === 'cancelling') return;
      void runsStore.cancel(activeRun.runId, useAuthStore().token).catch((error) => {
        alert(getPublicError(error).message);
      });
    },

    async resumeHitl(hitl: HitlRequest, answer: string) {
      if (!hitl.runId || !hitl.hitlToken) {
        alert('当前补充请求缺少可恢复的 Run 身份，请刷新对话后重试。');
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
      const assistant = this.ensureThreadMessages(run.threadId).find(
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

    async handleSend(options: SendOptions = {}) {
      const authStore = useAuthStore();
      const threadStore = useThreadStore();
      const runsStore = useRunsStore();
      const capabilityStore = useCapabilityStore();
      try {
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

        if (capabilityStore.selectedModeUnavailableReason) {
          alert(
            capabilityStore.selectedModeUnavailableReason === 'permission_required'
              ? '当前账号没有使用所选能力的权限，请切换模式。'
              : '所选能力的运行配置尚未就绪，请切换模式或稍后重试。'
          );
          return;
        }
        if (capabilityStore.selectedSkill?.approval_tools.length && !options.approvalConfirmed) {
          try {
            capabilityStore.openApproval();
          } catch (error) {
            alert(getPublicError(error).message);
          }
          return;
        }

        let executionMessage: CapabilityExecutionMessage;
        try {
          executionMessage = capabilityStore.composeExecutionMessage(text);
        } catch (error) {
          alert(getPublicError(error).message);
          return;
        }
        const selectedSkillName = capabilityStore.selectedSkill?.name || null;
        const selectedSkillVersion = capabilityStore.selectedSkill?.version || null;

        let threadId = this.threadId;
        if (!threadId) {
          try {
            const thread = await this.createNewThread(
              text.replace(/\s+/g, ' ').slice(0, 32),
              selectedSkillName
            );
            if (!thread) return;
            threadId = thread.thread_id;
          } catch (error) {
            const publicError = getPublicError(error);
            alert(publicError.message);
            return;
          }
        }
        const messages = this.ensureThreadMessages(threadId);
        const optimisticStartIndex = messages.length;
        const userMessage: Message = {
          text,
          isUser: true,
          status: 'completed',
          skillName: selectedSkillName,
          skillVersion: selectedSkillVersion,
        };
        const assistantMessage: Message = {
          text: '',
          isUser: false,
          isThinking: true,
          thinkingStartedAt: Date.now(),
          status: 'creating',
          skillName: selectedSkillName,
          skillVersion: selectedSkillVersion,
          ragTrace: null,
          ragSteps: [],
          runTimeline: [],
          artifacts: [],
          _groupedSteps: [],
        };
        messages.push(userMessage, assistantMessage);
        if (this.threadId === threadId) this.messages = messages;
        this.userInput = '';
        this.mergeCachedThreadsIntoHistory();

        try {
          const thread = threadStore.threads.find((item) => item.thread_id === threadId);
          const createPromise = runsStore.create({
            threadId,
            message: executionMessage.message,
            token: authStore.token,
            expectedThreadVersion: thread?.version,
            multitaskStrategy: 'reject',
            onDisconnect: 'continue',
            approvedTools: executionMessage.approvedTools,
          });
          this.mergeCachedThreadsIntoHistory();
          const created = await createPromise;
          capabilityStore.clearApprovalConfirmation();
          const runId = created.response.run.id;
          userMessage.id = created.response.run.user_message_id;
          userMessage.runId = runId;
          assistantMessage.id = created.response.run.assistant_message_id;
          assistantMessage.runId = runId;
          assistantMessage.status = created.response.run.status;
          if (thread) thread.version = created.response.thread_version;
          this.attachRunProjection(runId, threadId);
          this.projectRunState(created.state);
          await this.connectRun(runId, authStore.token);
        } catch (error) {
          const publicError = getPublicError(error);
          if (options.approvalConfirmed && !assistantMessage.runId) {
            const currentMessages = this.ensureThreadMessages(threadId);
            currentMessages.splice(optimisticStartIndex, 2);
            if (this.threadId === threadId) this.messages = currentMessages;
            this.userInput = text;
            throw publicError;
          }
          assistantMessage.isThinking = false;
          assistantMessage.status = 'failed';
          assistantMessage.text = appendPublicError(assistantMessage.text, publicError);
        } finally {
          this.mergeCachedThreadsIntoHistory();
        }
      } finally {
        if (options.approvalConfirmed) capabilityStore.clearApprovalConfirmation();
      }
    },

    removeThreadState(threadId: string) {
      const runsStore = useRunsStore();
      Object.values(runsStore.byId)
        .filter((run) => run.threadId === threadId)
        .forEach((run) => {
          projectionMap(this).get(run.runId)?.();
          projectionMap(this).delete(run.runId);
          runsStore.remove(run.runId);
        });
      delete this.messagesByThread[threadId];
      delete this.messagePagesByThread[threadId];
      useCapabilityStore().clearThreadSelection(threadId);
    },
  },
});
