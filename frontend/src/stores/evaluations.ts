import { defineStore } from 'pinia';
import {
  cancelRagEvaluationJob,
  createRagEvaluationDataset,
  createRagEvaluationJob,
  getRagEvaluationJob,
  listRagEvaluationCases,
  listRagEvaluationDatasets,
  listRagEvaluationJobs,
} from '@/evaluations/evaluationClient';
import type {
  RagEvaluationCaseResult,
  RagEvaluationDataset,
  RagEvaluationDatasetRecord,
  RagEvaluationJob,
  RagEvaluationJobCreatePayload,
  RagEvaluationJobStatus,
} from '@/types/evaluations';
import { getPublicError } from '@/utils/api';

const ACTIVE_STATUSES = new Set<RagEvaluationJobStatus>(['queued', 'running', 'cancelling']);

function schedulePoll(callback: () => void, delayMs: number): number {
  if (typeof window !== 'undefined') return window.setTimeout(callback, delayMs);
  return globalThis.setTimeout(callback, delayMs) as unknown as number;
}

function clearScheduledPoll(timer: number): void {
  if (typeof window !== 'undefined') window.clearTimeout(timer);
  else globalThis.clearTimeout(timer);
}

export const useEvaluationStore = defineStore('evaluations', {
  state: () => ({
    datasets: [] as RagEvaluationDatasetRecord[],
    jobs: [] as RagEvaluationJob[],
    casesByJob: {} as Record<string, RagEvaluationCaseResult[]>,
    selectedJobId: '',
    loading: false,
    saving: false,
    error: '',
    notice: '',
    pollTimer: null as number | null,
  }),

  getters: {
    selectedJob(state): RagEvaluationJob | null {
      return state.jobs.find((job) => job.id === state.selectedJobId) || null;
    },
    selectedCases(state): RagEvaluationCaseResult[] {
      return state.selectedJobId ? state.casesByJob[state.selectedJobId] || [] : [];
    },
    activeJobs(state): RagEvaluationJob[] {
      return state.jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
    },
    completedJobs(state): RagEvaluationJob[] {
      return state.jobs.filter((job) => job.status === 'succeeded' && job.report);
    },
  },

  actions: {
    async initialize() {
      this.loading = true;
      this.error = '';
      try {
        const [datasets, jobs] = await Promise.all([
          listRagEvaluationDatasets(),
          listRagEvaluationJobs(),
        ]);
        this.datasets = datasets;
        this.jobs = jobs;
        if (!this.selectedJobId && jobs.length) this.selectedJobId = jobs[0].id;
        if (this.selectedJobId) await this.refreshSelectedJob();
        this.startPolling();
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.loading = false;
      }
    },

    async refreshJobs() {
      const jobs = await listRagEvaluationJobs();
      this.jobs = jobs;
      if (this.selectedJobId && !jobs.some((job) => job.id === this.selectedJobId)) {
        this.selectedJobId = jobs[0]?.id || '';
      }
      return jobs;
    },

    async refreshSelectedJob() {
      if (!this.selectedJobId) return null;
      const [job, cases] = await Promise.all([
        getRagEvaluationJob(this.selectedJobId),
        listRagEvaluationCases(this.selectedJobId),
      ]);
      const index = this.jobs.findIndex((item) => item.id === job.id);
      if (index >= 0) this.jobs[index] = job;
      else this.jobs.unshift(job);
      this.casesByJob = { ...this.casesByJob, [job.id]: cases };
      return job;
    },

    async selectJob(jobId: string) {
      this.selectedJobId = jobId;
      return this.refreshSelectedJob();
    },

    async createDataset(dataset: RagEvaluationDataset) {
      return this.runMutation(async () => {
        const record = await createRagEvaluationDataset(dataset);
        const existing = this.datasets.findIndex((item) => item.id === record.id);
        if (existing >= 0) this.datasets[existing] = record;
        else this.datasets.unshift(record);
        this.notice = `已导入评估数据集「${record.name}」`;
        return record;
      });
    },

    async createJob(payload: RagEvaluationJobCreatePayload) {
      return this.runMutation(async () => {
        const job = await createRagEvaluationJob(payload);
        this.jobs.unshift(job);
        this.selectedJobId = job.id;
        this.casesByJob = { ...this.casesByJob, [job.id]: [] };
        this.notice = `Evaluation Job ${job.id.slice(-8)} 已排队`;
        this.startPolling();
        return job;
      });
    },

    async cancelJob(jobId: string) {
      return this.runMutation(async () => {
        const job = await cancelRagEvaluationJob(jobId);
        const index = this.jobs.findIndex((item) => item.id === job.id);
        if (index >= 0) this.jobs[index] = job;
        this.notice = '已请求取消 Evaluation Job';
        return job;
      });
    },

    async runMutation<T>(operation: () => Promise<T>) {
      this.saving = true;
      this.error = '';
      this.notice = '';
      try {
        return await operation();
      } catch (error) {
        const publicError = getPublicError(error);
        this.error = publicError.message;
        throw publicError;
      } finally {
        this.saving = false;
      }
    },

    startPolling() {
      if (this.pollTimer !== null || !this.activeJobs.length) return;
      this.pollTimer = schedulePoll(async () => {
        this.pollTimer = null;
        try {
          await this.refreshJobs();
          if (this.selectedJobId) await this.refreshSelectedJob();
        } catch {
          // Keep the last durable projection visible and retry on the next pass.
        } finally {
          if (this.activeJobs.length) this.startPolling();
        }
      }, 1800);
    },

    stopPolling() {
      if (this.pollTimer !== null) clearScheduledPoll(this.pollTimer);
      this.pollTimer = null;
    },

    reset() {
      this.stopPolling();
      this.$reset();
    },
  },
});
