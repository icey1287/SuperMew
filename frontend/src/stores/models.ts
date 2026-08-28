import { defineStore } from 'pinia';
import {
  assignModelRole,
  createModelProfile,
  deleteModelProfile,
  getModelControlPlane,
  updateModelProfile,
} from '@/models/modelClient';
import type {
  ModelControlPlane,
  ModelProfile,
  ModelProfilePayload,
  ModelRole,
} from '@/types/models';
import { getPublicError } from '@/utils/api';

export const useModelStore = defineStore('models', {
  state: () => ({
    controlPlane: null as ModelControlPlane | null,
    loading: false,
    saving: false,
    error: '',
    notice: '',
  }),

  getters: {
    profiles: (state): ModelProfile[] => state.controlPlane?.profiles || [],
    assignments: (state) => state.controlPlane?.assignments || null,
    apiKeyConfigured: (state): boolean => Boolean(state.controlPlane?.api_key_configured),
    readyForEvaluation(): boolean {
      if (!this.apiKeyConfigured || !this.assignments) return false;
      return ['answer', 'fast', 'grader', 'evaluator'].every((role) =>
        Boolean(this.assignments?.[role as ModelRole]?.enabled)
      );
    },
  },

  actions: {
    async fetchControlPlane() {
      this.loading = true;
      this.error = '';
      try {
        this.controlPlane = await getModelControlPlane();
        return this.controlPlane;
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.loading = false;
      }
    },

    async createProfile(payload: ModelProfilePayload) {
      return this.runMutation(
        () => createModelProfile(payload),
        `已创建 Model Profile「${payload.display_name}」`
      );
    },

    async updateProfile(profileId: string, payload: ModelProfilePayload) {
      return this.runMutation(
        () => updateModelProfile(profileId, payload),
        `已更新 Model Profile「${payload.display_name}」`
      );
    },

    async deleteProfile(profileId: string) {
      this.saving = true;
      this.error = '';
      try {
        await deleteModelProfile(profileId);
        await this.fetchControlPlane();
        this.notice = 'Model Profile 已删除';
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.saving = false;
      }
    },

    async assignRole(role: ModelRole, profileId: string) {
      return this.runMutation(
        () => assignModelRole(role, { profile_id: profileId }),
        `已更新 ${role} 模型；新 Assignment 仅影响后续 Run`
      );
    },

    async runMutation(operation: () => Promise<ModelControlPlane>, notice: string) {
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

    clearNotice() {
      this.notice = '';
    },

    reset() {
      this.$reset();
    },
  },
});
