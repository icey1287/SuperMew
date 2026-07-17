import { defineStore } from 'pinia';
import api from '@/utils/api';
import type {
  DocumentItem,
  UploadJob,
  UploadJobStatus,
  UploadStep,
  ActiveDeleteJob,
  DeleteJob,
  DeleteJobStatus,
  DeleteStep,
} from '@/types/document';

const ACTIVE_UPLOAD_JOB_STATUSES = new Set<UploadJobStatus>([
  'pending',
  'running',
  'retry_wait',
  'staged',
]);

const TERMINAL_UPLOAD_JOB_STATUSES = new Set<UploadJobStatus>([
  'completed',
  'failed',
  'cancelled',
  'dead_letter',
]);

const RECOVERABLE_DELETE_JOB_STATUSES = new Set<DeleteJobStatus>([
  'running',
  'cleanup_failed',
  'failed',
]);

function retryMessage(message: string, nextRetryAt?: string | null): string {
  if (!nextRetryAt) return message;
  const timestamp = new Date(nextRetryAt);
  if (Number.isNaN(timestamp.getTime())) return message;
  const formatted = new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZone: 'Asia/Shanghai',
  }).format(timestamp);
  return `${message}；下次重试：${formatted}`;
}

const jobTimestamp = (job: { updated_at?: string; created_at?: string }): number => {
  const value = Date.parse(job.created_at || job.updated_at || '');
  return Number.isFinite(value) ? value : 0;
};

const latestJobsByKey = <T extends { job_id: string; updated_at?: string; created_at?: string }>(
  jobs: T[],
  keyFor: (job: T) => string
): T[] => {
  const latest = new Map<string, T>();
  [...jobs]
    .sort((left, right) => jobTimestamp(right) - jobTimestamp(left))
    .forEach((job) => {
      const key = keyFor(job);
      if (key && !latest.has(key)) {
        latest.set(key, job);
      }
    });
  return [...latest.values()];
};

export const useDocumentStore = defineStore('documents', {
  state: () => ({
    documents: [] as DocumentItem[],
    documentsLoading: false,
    workspaceNotice: '',
    selectedFile: null as File | null,
    isUploading: false,
    uploadProgress: '',
    uploadSteps: [] as UploadStep[],
    uploadProgressCollapsed: false,
    activeUploadJobId: '',
    uploadPollTimer: null as any,
    deleteJobs: {} as Record<string, ActiveDeleteJob>,
    deletePollTimers: {} as Record<string, any>,
  }),

  actions: {
    createUploadSteps(): UploadStep[] {
      return [
        { key: 'upload', label: '文档上传', percent: 0, status: 'pending', message: '' },
        { key: 'reserve', label: '候选版本准备', percent: 0, status: 'pending', message: '' },
        { key: 'parse', label: '解析与版本化分块', percent: 0, status: 'pending', message: '' },
        {
          key: 'parent_store',
          label: '候选父级分块写入',
          percent: 0,
          status: 'pending',
          message: '',
        },
        { key: 'vector_store', label: '候选向量写入', percent: 0, status: 'pending', message: '' },
        { key: 'verify', label: '索引一致性核验', percent: 0, status: 'pending', message: '' },
        { key: 'publish', label: '原子发布新版本', percent: 0, status: 'pending', message: '' },
      ];
    },

    createDeleteSteps(): DeleteStep[] {
      return [
        { key: 'prepare', label: '原子撤销检索范围', percent: 0, status: 'pending', message: '' },
        { key: 'milvus', label: '清理向量索引', percent: 0, status: 'pending', message: '' },
        {
          key: 'parent_store',
          label: '清理父级分块与缓存',
          percent: 0,
          status: 'pending',
          message: '',
        },
        { key: 'object_store', label: '清理版本对象', percent: 0, status: 'pending', message: '' },
        { key: 'finalize', label: '确认清理状态', percent: 0, status: 'pending', message: '' },
      ];
    },

    updateUploadStep(
      key: string,
      percent: number,
      status: UploadStep['status'] = 'running',
      message = ''
    ) {
      if (!this.uploadSteps.length) {
        this.uploadSteps = this.createUploadSteps();
      }
      const idx = this.uploadSteps.findIndex((step) => step.key === key);
      if (idx === -1) return;
      this.uploadSteps[idx] = {
        ...this.uploadSteps[idx],
        percent: Math.max(0, Math.min(100, Math.round(percent || 0))),
        status,
        message,
      };
    },

    mergeDocumentsWithActiveDeletes(nextDocuments: DocumentItem[]): DocumentItem[] {
      const merged = Array.isArray(nextDocuments) ? [...nextDocuments] : [];
      Object.keys(this.deleteJobs).forEach((filename) => {
        const job = this.deleteJobs[filename];
        if (!job || job.status === 'failed') return;
        const exists = merged.some((doc) => doc.filename === filename);
        if (!exists) {
          const currentDoc = this.documents.find((doc) => doc.filename === filename);
          if (currentDoc) {
            merged.push(currentDoc);
          }
        }
      });
      return merged;
    },

    async loadDocuments() {
      this.documentsLoading = true;
      this.workspaceNotice = '';
      try {
        const response = await api.get('/documents');
        this.documents = this.mergeDocumentsWithActiveDeletes(response.data.documents || []);
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '加载文档列表失败';
        throw new Error(errMsg);
      } finally {
        this.documentsLoading = false;
      }
    },

    async initializeDocumentWorkspace() {
      const initialResults = await Promise.allSettled([
        this.loadDocuments(),
        this.restoreDurableUploadJob(),
      ]);
      // Retirement fencing compares the durable operation time with the current
      // catalog version, so restore it only after the latest document list settles.
      const deleteResults = await Promise.allSettled([this.restoreDurableDeleteJobs()]);
      const results = [...initialResults, ...deleteResults];
      const rejected = results.find(
        (result): result is PromiseRejectedResult => result.status === 'rejected'
      );
      if (rejected) {
        const reason = rejected.reason;
        throw reason instanceof Error ? reason : new Error(String(reason || '知识库同步失败'));
      }
    },

    async restoreDurableUploadJob() {
      const response = await api.get('/documents/upload/jobs');
      const jobs = Array.isArray(response.data) ? (response.data as UploadJob[]) : [];
      const latestPerDocument = latestJobsByKey(jobs, (job) => job.filename || job.job_id);
      const activeJob = latestPerDocument
        .filter((job) => ACTIVE_UPLOAD_JOB_STATUSES.has(job.status))
        .sort((left, right) => jobTimestamp(right) - jobTimestamp(left))[0];

      if (!activeJob) return;

      this.isUploading = true;
      this.selectedFile = null;
      this.uploadProgressCollapsed = false;
      this.syncUploadJob(activeJob);
      this.startUploadJobPolling(activeJob.job_id);
    },

    async restoreDurableDeleteJobs() {
      const response = await api.get('/documents/delete/jobs');
      const jobs = Array.isArray(response.data) ? (response.data as DeleteJob[]) : [];
      const latestPerDocument = latestJobsByKey(
        jobs,
        (job) => job.filename || job.document_id || job.job_id
      );

      latestPerDocument.forEach((job) => {
        const filename = job.filename?.trim();
        if (!filename) return;

        if (this.isRetirementSupersededByLiveDocument(job)) {
          this.stopDeleteJobPolling(filename);
          const { [filename]: _stale, ...remaining } = this.deleteJobs;
          this.deleteJobs = remaining;
          return;
        }
        if (job.status === 'completed') {
          if (this.deleteJobs[filename]) {
            this.syncDeleteJob(filename, job);
            void this.finalizeDeletedDocument(filename);
          }
          return;
        }
        if (!RECOVERABLE_DELETE_JOB_STATUSES.has(job.status)) return;

        this.syncDeleteJob(filename, job);
        this.ensureRecoveredDeleteDocument(job);
        if (job.status === 'running') {
          this.startDeleteJobPolling(filename, job.job_id);
        } else {
          this.stopDeleteJobPolling(filename);
        }
      });
    },

    isRetirementSupersededByLiveDocument(job: DeleteJob): boolean {
      if (job.status === 'failed') return false;
      const document = this.documents.find((item) => item.filename === job.filename);
      if (!document?.uploaded_at || !job.created_at) return false;
      const documentTimestamp = Date.parse(document.uploaded_at);
      const retirementTimestamp = Date.parse(job.created_at);
      return (
        Number.isFinite(documentTimestamp) &&
        Number.isFinite(retirementTimestamp) &&
        documentTimestamp > retirementTimestamp
      );
    },

    ensureRecoveredDeleteDocument(job: DeleteJob) {
      if (this.documents.some((document) => document.filename === job.filename)) return;
      const suffix = job.filename.split('.').pop()?.toLowerCase();
      const fileType =
        suffix === 'pdf'
          ? 'PDF'
          : suffix === 'doc' || suffix === 'docx'
            ? 'Word'
            : suffix === 'xls' || suffix === 'xlsx'
              ? 'Excel'
              : suffix === 'html' || suffix === 'htm'
                ? 'HTML'
                : 'Document';
      this.documents = [
        ...this.documents,
        {
          filename: job.filename,
          file_type: fileType,
          chunk_count: 0,
          document_id: job.document_id || undefined,
          current_version_id: null,
          pending_version_id: null,
          status: 'deleted',
        },
      ];
    },

    async uploadDocument() {
      if (!this.selectedFile) {
        throw new Error('请先选择文件');
      }

      this.isUploading = true;
      this.uploadProgress = '正在上传...';
      this.uploadSteps = this.createUploadSteps();
      this.uploadProgressCollapsed = false;
      this.updateUploadStep('upload', 0, 'running', '准备上传');

      const formData = new FormData();
      formData.append('file', this.selectedFile);

      try {
        const response = await api.post('/documents/upload/async', formData, {
          headers: {
            'Content-Type': 'multipart/form-data',
          },
          onUploadProgress: (progressEvent) => {
            if (!progressEvent.total) return;
            const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
            this.updateUploadStep('upload', percent, 'running', `已上传 ${percent}%`);
          },
        });

        const data = response.data;
        this.updateUploadStep('upload', 100, 'completed', '文档上传完成');
        this.uploadProgress = data.message;
        this.activeUploadJobId = data.job_id;
        this.startUploadJobPolling(data.job_id);
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '上传失败';
        this.updateUploadStep('upload', 100, 'failed', errMsg);
        this.uploadProgress = '上传失败：' + errMsg;
        this.isUploading = false;
        throw new Error(errMsg);
      }
    },

    syncUploadJob(job: UploadJob) {
      this.activeUploadJobId = job.job_id;
      this.uploadProgress = retryMessage(job.message || '', job.next_retry_at);
      if (Array.isArray(job.steps)) {
        this.uploadSteps = job.steps.map((step: any) => ({
          key: step.key,
          label: step.label,
          percent: step.percent,
          status: step.status,
          message: step.message || '',
        }));
      }
      if (job.status === 'completed') {
        this.uploadProgressCollapsed = true;
      }
    },

    startUploadJobPolling(jobId: string) {
      this.stopUploadJobPolling();
      this.activeUploadJobId = jobId;
      let pollInFlight = false;

      const poll = async () => {
        if (pollInFlight || this.activeUploadJobId !== jobId) return;
        pollInFlight = true;
        try {
          const response = await api.get(`/documents/upload/jobs/${encodeURIComponent(jobId)}`);
          if (this.activeUploadJobId !== jobId) return;

          const job = response.data as UploadJob;
          this.syncUploadJob(job);

          if (job.status === 'completed') {
            this.stopUploadJobPolling();
            this.isUploading = false;
            this.selectedFile = null;
            try {
              await this.loadDocuments();
            } catch (error: any) {
              if (this.activeUploadJobId === jobId) {
                const detail = error.message || '目录刷新失败';
                this.uploadProgress = `${job.message || '文档版本已发布'}；目录刷新失败：${detail}`;
              }
            }
          } else if (TERMINAL_UPLOAD_JOB_STATUSES.has(job.status)) {
            this.stopUploadJobPolling();
            this.isUploading = false;
          }
        } catch (error: any) {
          if (this.activeUploadJobId !== jobId) return;
          const detail = error.response?.data?.detail || error.message || '网络不可用';
          this.uploadProgress = `进度连接中断：${detail}；后台任务仍在继续，1 秒后自动重试`;
        } finally {
          pollInFlight = false;
        }
      };

      this.uploadPollTimer = setInterval(() => void poll(), 1000);
      void poll();
    },

    stopUploadJobPolling() {
      if (this.uploadPollTimer) {
        clearInterval(this.uploadPollTimer);
        this.uploadPollTimer = null;
      }
    },

    isDeletingDocument(filename: string): boolean {
      const job = this.deleteJobs[filename];
      return !!(job && job.status === 'running');
    },

    isDeleteActionLocked(filename: string): boolean {
      const job = this.deleteJobs[filename];
      return !!(
        job &&
        (job.status === 'running' || job.status === 'completed' || job.status === 'cleanup_failed')
      );
    },

    getDeleteButtonIcon(filename: string): string {
      const job = this.deleteJobs[filename];
      if (job?.status === 'running') return 'fas fa-spinner fa-spin';
      if (job?.status === 'completed') return 'fas fa-check';
      if (job?.status === 'cleanup_failed') return 'fas fa-triangle-exclamation';
      return 'fas fa-trash';
    },

    setDeleteJob(filename: string, nextJob: Partial<ActiveDeleteJob>) {
      this.deleteJobs = {
        ...this.deleteJobs,
        [filename]: {
          ...(this.deleteJobs[filename] || {
            status: 'running',
            message: '',
            collapsed: false,
            steps: this.createDeleteSteps(),
          }),
          ...nextJob,
        },
      };
    },

    syncDeleteJob(filename: string, job: DeleteJob) {
      const current = this.deleteJobs[filename] || {};
      if (
        current.jobId === job.job_id &&
        current.status !== 'running' &&
        job.status === 'running'
      ) {
        return;
      }
      this.setDeleteJob(filename, {
        jobId: job.job_id,
        documentId: job.document_id,
        documentVersionId: job.document_version_id,
        deadLetterJobIds: Array.isArray(job.dead_letter_job_ids)
          ? [...job.dead_letter_job_ids]
          : [],
        createdAt: job.created_at,
        updatedAt: job.updated_at,
        nextRetryAt: job.next_retry_at,
        status: job.status,
        message: retryMessage(job.message || '', job.next_retry_at),
        collapsed:
          job.status === 'completed' || job.status === 'cleanup_failed'
            ? true
            : Boolean(current.collapsed),
        steps: Array.isArray(job.steps)
          ? job.steps.map((step: any) => ({
              key: step.key,
              label: step.label,
              percent: step.percent,
              status: step.status,
              message: step.message || '',
            }))
          : this.createDeleteSteps(),
      });
    },

    async deleteDocument(filename: string) {
      if (this.isDeleteActionLocked(filename)) {
        return;
      }
      if (!confirm(`确定要删除文档 "${filename}" 吗？这将同时删除 Milvus 中的所有相关向量。`)) {
        return;
      }

      this.setDeleteJob(filename, {
        status: 'running',
        message: '正在提交删除任务...',
        collapsed: false,
        steps: this.createDeleteSteps().map((step) =>
          step.key === 'prepare'
            ? { ...step, percent: 1, status: 'running' as const, message: '正在提交删除任务' }
            : step
        ),
      });

      try {
        const response = await api.delete(
          `/documents/delete/async/${encodeURIComponent(filename)}`
        );
        const data = response.data;
        this.setDeleteJob(filename, {
          jobId: data.job_id,
          status: 'running',
          message: data.message || `正在删除 ${filename}`,
          collapsed: false,
        });
        this.startDeleteJobPolling(filename, data.job_id);
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '删除请求失败';
        this.setDeleteJob(filename, {
          status: 'failed',
          message: '删除文档失败：' + errMsg,
          collapsed: false,
          steps: this.deleteJobs[filename]?.steps || this.createDeleteSteps(),
        });
      }
    },

    startDeleteJobPolling(filename: string, jobId: string) {
      this.stopDeleteJobPolling(filename);
      let pollInFlight = false;

      const poll = async () => {
        if (pollInFlight || this.deleteJobs[filename]?.jobId !== jobId) return;
        pollInFlight = true;
        try {
          const response = await api.get(`/documents/delete/jobs/${encodeURIComponent(jobId)}`);
          if (this.deleteJobs[filename]?.jobId !== jobId) return;
          const job = response.data;
          this.syncDeleteJob(filename, job);

          if (job.status === 'completed') {
            this.stopDeleteJobPolling(filename);
            void this.finalizeDeletedDocument(filename);
          } else if (job.status === 'cleanup_failed') {
            this.stopDeleteJobPolling(filename);
          } else if (job.status === 'failed') {
            this.stopDeleteJobPolling(filename);
          }
        } catch (error: any) {
          if (this.deleteJobs[filename]?.jobId !== jobId) return;
          const errMsg = error.response?.data?.detail || error.message || '查询失败';
          this.setDeleteJob(filename, {
            status: 'running',
            message: `删除进度连接中断：${errMsg}；后台清理仍在继续，1 秒后自动重试`,
            collapsed: false,
            steps: this.deleteJobs[filename]?.steps || this.createDeleteSteps(),
          });
        } finally {
          pollInFlight = false;
        }
      };

      this.deletePollTimers = {
        ...this.deletePollTimers,
        [filename]: setInterval(() => void poll(), 1000),
      };
      void poll();
    },

    stopDeleteJobPolling(filename: string) {
      const timer = this.deletePollTimers[filename];
      if (timer == null) return;
      clearInterval(timer);
      const { [filename]: _, ...rest } = this.deletePollTimers;
      this.deletePollTimers = rest;
    },

    stopAllDeleteJobPolling() {
      Object.keys(this.deletePollTimers).forEach((filename) => this.stopDeleteJobPolling(filename));
    },

    async finalizeDeletedDocument(filename: string) {
      this.documents = this.documents.filter((doc) => doc.filename !== filename);
      const { [filename]: _job, ...jobs } = this.deleteJobs;
      this.deleteJobs = jobs;
      try {
        await this.loadDocuments();
      } catch (error: any) {
        const detail = error?.message || '目录刷新失败';
        this.workspaceNotice = `文档已删除，但目录刷新失败：${detail}`;
      }
    },

    toggleDeleteJobCollapsed(filename: string) {
      const job = this.deleteJobs[filename];
      if (!job) return;
      this.setDeleteJob(filename, { collapsed: !job.collapsed });
    },
  },
});
export type { DocumentItem };
