import { defineStore } from 'pinia';
import { getCapabilityCatalog } from '@/capabilities/capabilityClient';
import { skillDisplayName, skillDisplaySummary } from '@/capabilities/skillPresentation';
import type {
  CapabilityApprovalDraft,
  CapabilityAvailabilityFilter,
  CapabilityAvailabilityReason,
  CapabilityCatalogResponse,
  CapabilityExecutionMessage,
  CapabilitySkill,
  CapabilityTool,
  SandboxLanguage,
} from '@/types/capabilities';
import { getPublicError } from '@/utils/api';

function capabilityError(
  code:
    | 'CONFLICT'
    | 'INVALID_REQUEST'
    | 'NOT_FOUND'
    | 'PERMISSION_DENIED'
    | 'POLICY_DENIED'
    | 'TOOL_UNAVAILABLE',
  message: string,
  retryable = false
) {
  return getPublicError({
    code,
    message,
    retryable,
    category: 'capability',
  });
}

function normalizedThreadId(threadId: string | null | undefined): string | null {
  const normalized = threadId?.trim();
  return normalized || null;
}

function prepareApprovalDraft(skill: CapabilitySkill | null): CapabilityApprovalDraft | null {
  if (!skill?.approval_tools.length || !skill.available) return null;
  return {
    skillName: skill.name,
    toolNames: [...skill.approval_tools],
    confirmed: false,
  };
}

function approvalDraftMatches(
  draft: CapabilityApprovalDraft | null,
  skill: CapabilitySkill | null
): boolean {
  return Boolean(
    draft &&
    skill?.available &&
    draft.skillName === skill.name &&
    draft.toolNames.length === skill.approval_tools.length &&
    skill.approval_tools.every((name) => draft.toolNames.includes(name))
  );
}

export const useCapabilityStore = defineStore('capabilities', {
  state: () => ({
    catalog: null as CapabilityCatalogResponse | null,
    loading: false,
    error: '',
    centerOpen: false,
    paletteOpen: false,
    approvalOpen: false,
    searchQuery: '',
    availabilityFilter: 'all' as CapabilityAvailabilityFilter,
    selectedSkillName: null as string | null,
    activeThreadId: null as string | null,
    selectedSkillByThread: {} as Record<string, string | null>,
    sandboxLanguage: 'python' as SandboxLanguage,
    pendingApprovalDraft: null as CapabilityApprovalDraft | null,
  }),

  getters: {
    skills: (state): CapabilitySkill[] => state.catalog?.skills || [],
    tools: (state): CapabilityTool[] => state.catalog?.tools || [],
    isEmpty: (state): boolean =>
      Boolean(
        !state.loading &&
        state.catalog &&
        state.catalog.skills.length === 0 &&
        state.catalog.tools.length === 0
      ),
    selectedSkill(state): CapabilitySkill | null {
      if (!state.selectedSkillName) return null;
      return state.catalog?.skills.find((skill) => skill.name === state.selectedSkillName) || null;
    },
    selectedTools(): CapabilityTool[] {
      const selected = this.selectedSkill;
      if (!selected) return [];
      const toolNames = new Set(selected.tool_names);
      return this.tools.filter((tool) => toolNames.has(tool.name));
    },
    selectedModeUnavailableReason(state): CapabilityAvailabilityReason {
      if (!state.selectedSkillName) return null;
      const selected = state.catalog?.skills.find(
        (skill) => skill.name === state.selectedSkillName
      );
      if (!selected) return 'not_configured';
      return selected.available ? null : selected.availability_reason || 'not_configured';
    },
    filteredSkills(state): CapabilitySkill[] {
      const query = state.searchQuery.trim().toLocaleLowerCase();
      return (state.catalog?.skills || []).filter((skill) => {
        if (state.availabilityFilter === 'available' && !skill.available) return false;
        if (state.availabilityFilter === 'unavailable' && skill.available) return false;
        if (!query) return true;
        return [
          skill.name,
          skill.description,
          skill.activation,
          skillDisplayName(skill.name, skill.description),
          skillDisplaySummary(skill),
          ...skill.tool_names,
        ]
          .join(' ')
          .toLocaleLowerCase()
          .includes(query);
      });
    },
    selectedApprovedTools(state): string[] {
      const selected = state.catalog?.skills.find(
        (skill) => skill.name === state.selectedSkillName
      );
      const draft = state.pendingApprovalDraft;
      if (!selected || !draft?.confirmed || draft.skillName !== selected.name) return [];
      const approved = new Set(draft.toolNames);
      return selected.approval_tools.filter((name) => approved.has(name));
    },
  },

  actions: {
    async fetchCatalog() {
      this.loading = true;
      this.error = '';
      try {
        const catalog = await getCapabilityCatalog();
        this.catalog = catalog;
        const selected = this.selectedSkill;
        if (!approvalDraftMatches(this.pendingApprovalDraft, selected)) {
          this.pendingApprovalDraft = prepareApprovalDraft(selected);
        }
        return catalog;
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.loading = false;
      }
    },

    retryCatalog() {
      return this.fetchCatalog();
    },

    openCenter() {
      this.centerOpen = true;
      this.paletteOpen = false;
      this.approvalOpen = false;
    },

    closeCenter() {
      this.centerOpen = false;
    },

    toggleCenter() {
      if (this.centerOpen) this.closeCenter();
      else this.openCenter();
    },

    openPalette() {
      this.paletteOpen = true;
      this.centerOpen = false;
      this.approvalOpen = false;
    },

    closePalette() {
      this.paletteOpen = false;
    },

    togglePalette() {
      if (this.paletteOpen) this.closePalette();
      else this.openPalette();
    },

    openApproval() {
      const draft = this.pendingApprovalDraft;
      if (!draft || !draft.toolNames.length || draft.confirmed) {
        throw capabilityError('POLICY_DENIED', '当前没有待确认的工具审批。');
      }
      this.approvalOpen = true;
      this.centerOpen = false;
      this.paletteOpen = false;
    },

    closeApproval() {
      this.approvalOpen = false;
    },

    setSearchQuery(query: string) {
      this.searchQuery = query;
    },

    setAvailabilityFilter(filter: CapabilityAvailabilityFilter) {
      this.availabilityFilter = filter;
    },

    setActiveThread(threadId: string | null) {
      this.activeThreadId = normalizedThreadId(threadId);
      this.selectedSkillName = this.activeThreadId
        ? (this.selectedSkillByThread[this.activeThreadId] ?? null)
        : null;
      this.pendingApprovalDraft = prepareApprovalDraft(this.selectedSkill);
    },

    selectSkill(skillName: string | null, threadId?: string | null) {
      const normalizedName = skillName?.trim() || null;
      if (normalizedName) {
        if (!this.catalog) {
          throw capabilityError('CONFLICT', '能力目录尚未加载，请刷新后重试。', true);
        }
        const skill = this.catalog.skills.find((item) => item.name === normalizedName);
        if (!skill) {
          throw capabilityError('NOT_FOUND', '所选能力不存在或已被移除。');
        }
        if (!skill.available) {
          const code =
            skill.availability_reason === 'permission_required'
              ? 'PERMISSION_DENIED'
              : 'TOOL_UNAVAILABLE';
          throw capabilityError(code, '所选能力当前不可用，不能启动 Run。');
        }
      }

      this.selectedSkillName = normalizedName;
      const effectiveThreadId = normalizedThreadId(
        threadId === undefined ? this.activeThreadId : threadId
      );
      if (effectiveThreadId) {
        this.selectedSkillByThread = {
          ...this.selectedSkillByThread,
          [effectiveThreadId]: normalizedName,
        };
      }
      this.pendingApprovalDraft = prepareApprovalDraft(this.selectedSkill);
    },

    restoreThreadSkill(skillName: string | null | undefined, threadId: string) {
      const normalizedThread = normalizedThreadId(threadId);
      if (!normalizedThread) return;
      const normalizedName = skillName?.trim() || null;
      this.selectedSkillByThread = {
        ...this.selectedSkillByThread,
        [normalizedThread]: normalizedName,
      };
      if (this.activeThreadId === normalizedThread) {
        this.selectedSkillName = normalizedName;
        this.pendingApprovalDraft = prepareApprovalDraft(this.selectedSkill);
      }
    },

    clearThreadSelection(threadId: string) {
      const normalized = normalizedThreadId(threadId);
      if (!normalized) return;
      const { [normalized]: _removed, ...remaining } = this.selectedSkillByThread;
      this.selectedSkillByThread = remaining;
      if (this.activeThreadId === normalized) {
        this.selectedSkillName = null;
        this.pendingApprovalDraft = null;
      }
    },

    setSandboxLanguage(language: SandboxLanguage) {
      if (language !== 'python' && language !== 'sh') {
        throw capabilityError('INVALID_REQUEST', 'Sandbox 只支持 python 或 sh。');
      }
      this.sandboxLanguage = language;
    },

    confirmPendingApproval() {
      const selected = this.selectedSkill;
      const draft = this.pendingApprovalDraft;
      if (
        !selected ||
        !selected.available ||
        !selected.approval_tools.length ||
        !draft ||
        draft.skillName !== selected.name ||
        draft.toolNames.length !== selected.approval_tools.length ||
        !selected.approval_tools.every((name) => draft.toolNames.includes(name))
      ) {
        throw capabilityError('POLICY_DENIED', '当前没有可确认的工具审批。');
      }
      this.pendingApprovalDraft = { ...draft, confirmed: true };
      this.approvalOpen = false;
    },

    clearApprovalConfirmation() {
      if (this.pendingApprovalDraft) {
        this.pendingApprovalDraft = { ...this.pendingApprovalDraft, confirmed: false };
      }
      this.approvalOpen = false;
    },

    composeExecutionMessage(userText: string): CapabilityExecutionMessage {
      const source = userText.trim();
      if (!source) {
        throw capabilityError('INVALID_REQUEST', '请输入要执行的内容。');
      }
      if (!this.selectedSkillName) {
        return { message: source, approvedTools: [] };
      }

      const selected = this.selectedSkill;
      if (!selected || !selected.available || this.selectedModeUnavailableReason) {
        throw capabilityError('PERMISSION_DENIED', '所选能力当前不可用，不能启动 Run。');
      }
      if (
        selected.approval_tools.length &&
        (!this.pendingApprovalDraft?.confirmed ||
          this.pendingApprovalDraft.skillName !== selected.name)
      ) {
        throw capabilityError('PERMISSION_DENIED', '该能力需要先确认高风险工具审批。');
      }

      const message =
        selected.name === 'sandbox'
          ? `/sandbox\n请调用 sandbox_execute，并严格使用以下 JSON 参数执行隔离代码：\n${JSON.stringify(
              { language: this.sandboxLanguage, source },
              null,
              2
            )}`
          : `/${selected.name}\n${source}`;
      return {
        message,
        approvedTools: [...this.selectedApprovedTools],
      };
    },

    reset() {
      this.$reset();
    },
  },
});
