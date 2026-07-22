import { defineStore } from 'pinia';
import {
  createManagedSkill,
  createManagedTool,
  deleteManagedSkill,
  deleteManagedTool,
  getCapabilityControlPlane,
  updateManagedSkill,
  updateManagedTool,
  updateSqlAssistantConfig,
  updateWebResearchConfig,
} from '@/capabilities/capabilityClient';
import type {
  CapabilityControlPlane,
  ManagedHttpTool,
  ManagedHttpToolPayload,
  ManagedSkill,
  ManagedSkillPayload,
  SqlAssistantConfigPayload,
} from '@/types/capabilities';
import { getPublicError } from '@/utils/api';

export const useCapabilityAdminStore = defineStore('capability-admin', {
  state: () => ({
    controlPlane: null as CapabilityControlPlane | null,
    loading: false,
    saving: false,
    error: '',
    notice: '',
  }),

  getters: {
    skills: (state): ManagedSkill[] => state.controlPlane?.skills || [],
    customTools: (state): ManagedHttpTool[] => state.controlPlane?.custom_tools || [],
    availableToolNames(state): string[] {
      if (!state.controlPlane) return [];
      return [
        ...state.controlPlane.builtin_tools.map((tool) => tool.name),
        ...state.controlPlane.custom_tools.map((tool) => tool.name),
      ].sort((left, right) => left.localeCompare(right));
    },
  },

  actions: {
    async fetchControlPlane() {
      this.loading = true;
      this.error = '';
      try {
        this.controlPlane = await getCapabilityControlPlane();
        return this.controlPlane;
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.loading = false;
      }
    },

    createSkill(name: string, payload: ManagedSkillPayload) {
      return this.runMutation(
        () => createManagedSkill({ name, ...payload }),
        `已创建 Skill「${name}」`
      );
    },

    updateSkill(name: string, payload: ManagedSkillPayload) {
      return this.runMutation(() => updateManagedSkill(name, payload), `已更新 Skill「${name}」`);
    },

    async deleteSkill(name: string) {
      return this.runDelete(() => deleteManagedSkill(name), `已删除 Skill「${name}」`);
    },

    createTool(name: string, payload: ManagedHttpToolPayload) {
      return this.runMutation(
        () => createManagedTool({ name, ...payload }),
        `已创建 Tool「${name}」`
      );
    },

    updateTool(name: string, payload: ManagedHttpToolPayload) {
      return this.runMutation(() => updateManagedTool(name, payload), `已更新 Tool「${name}」`);
    },

    async deleteTool(name: string) {
      return this.runDelete(() => deleteManagedTool(name), `已删除 Tool「${name}」`);
    },

    updateSqlAssistant(payload: SqlAssistantConfigPayload) {
      return this.runMutation(() => updateSqlAssistantConfig(payload), 'SQL Assistant 配置已保存');
    },

    updateWebResearch(enabled: boolean) {
      return this.runMutation(
        () => updateWebResearchConfig(enabled),
        `Tavily Keyless Web Research 已${enabled ? '启用' : '停用'}`
      );
    },

    async runMutation(
      operation: () => Promise<CapabilityControlPlane>,
      notice: string
    ): Promise<CapabilityControlPlane> {
      this.saving = true;
      this.error = '';
      this.notice = '';
      try {
        this.controlPlane = await operation();
        this.notice = notice;
        return this.controlPlane;
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.saving = false;
      }
    },

    async runDelete(operation: () => Promise<unknown>, notice: string) {
      this.saving = true;
      this.error = '';
      this.notice = '';
      try {
        await operation();
        this.controlPlane = await getCapabilityControlPlane();
        this.notice = notice;
        return this.controlPlane;
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.saving = false;
      }
    },

    clearNotice() {
      this.notice = '';
    },

    reset() {
      this.$reset();
    },
  },
});
