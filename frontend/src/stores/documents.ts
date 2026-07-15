import { defineStore } from 'pinia';
import api from '@/utils/api';
import type {
  DocumentItem,
  UploadJob,
  UploadJobStatus,
  UploadStep,
  ActiveDeleteJob,
  DeleteStep,
} from '@/types/document';

const TERMINAL_UPLOAD_JOB_STATUSES = new Set<UploadJobStatus>([
  'completed',
  'failed',
  'cancelled',
  'dead_letter',
]);

export const useDocumentStore = defineStore('documents', {
  state: () => ({
    documents: [] as DocumentItem[],
    documentsLoading: false,
    selectedFile: null as File | null,
    isUploading: false,
    uploadProgress: '',
    uploadSteps: [] as UploadStep[],
    uploadProgressCollapsed: false,
    activeUploadJobId: '',
    uploadPollTimer: null as any,
    deleteJobs: {} as Record<string, ActiveDeleteJob>,
    deletePollTimers: {} as Record<string, any>,
    deleteRemoveTimers: {} as Record<string, any>,
  }),

  actions: {
    createUploadSteps(): UploadStep[] {
      return [
        { key: 'upload', label: '文档上传', percent: 0, status: 'pending', message: '' },
        { key: 'reserve', label: '候选版本准备', percent: 0, status: 'pending', message: '' },
        { key: 'parse', label: '解析与版本化分块', percent: 0, status: 'pending', message: '' },
        { key: 'parent_store', label: '候选父级分块写入', percent: 0, status: 'pending', message: '' },
        { key: 'vector_store', label: '候选向量写入', percent: 0, status: 'pending', message: '' },
        { key: 'verify', label: '索引一致性核验', percent: 0, status: 'pending', message: '' },
        { key: 'publish', label: '原子发布新版本', percent: 0, status: 'pending', message: '' },
      ];
    },

    createDeleteSteps(): DeleteStep[] {
      return [
        { key: 'prepare', label: '原子撤销检索范围', percent: 0, status: 'pending', message: '' },
        { key: 'milvus', label: '清理向量索引', percent: 0, status: 'pending', message: '' },
        { key: 'parent_store', label: '清理父级分块与缓存', percent: 0, status: 'pending', message: '' },
        { key: 'object_store', label: '清理版本对象', percent: 0, status: 'pending', message: '' },
        { key: 'finalize', label: '确认清理状态', percent: 0, status: 'pending', message: '' },
      ];
    },

    updateUploadStep(key: string, percent: number, status: UploadStep['status'] = 'running', message = '') {
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
      this.uploadProgress = job.message || '';
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
          this.uploadProgress = '进度查询失败：' + (error.response?.data?.detail || error.message);
          this.stopUploadJobPolling();
          this.isUploading = false;
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
        (job.status === 'running' || job.status === 'completed')
      );
    },

    getDeleteButtonIcon(filename: string): string {
      const job = this.deleteJobs[filename];
      if (job?.status === 'running') return 'fas fa-spinner fa-spin';
      if (job?.status === 'completed') return 'fas fa-check';
      if (job?.status === 'cleanup_pending') return 'fas fa-triangle-exclamation';
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

    syncDeleteJob(filename: string, job: any) {
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
        status: job.status,
        message: job.message || '',
        collapsed:
          job.status === 'completed' || job.status === 'cleanup_pending'
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
      if (this.isDeletingDocument(filename)) {
        return;
      }
      if (!confirm(`确定要删除文档 "${filename}" 吗？这将同时删除 Milvus 中的所有相关向量。`)) {
        return;
      }

      this.clearDeleteRemovalTimer(filename);
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
        const response = await api.delete(`/documents/delete/async/${encodeURIComponent(filename)}`);
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
            this.scheduleDeletedDocumentRemoval(filename);
          } else if (job.status === 'cleanup_pending') {
            this.stopDeleteJobPolling(filename);
          } else if (job.status === 'failed') {
            this.stopDeleteJobPolling(filename);
          }
        } catch (error: any) {
          if (this.deleteJobs[filename]?.jobId !== jobId) return;
          const errMsg = error.response?.data?.detail || error.message || '查询失败';
          this.setDeleteJob(filename, {
            status: 'failed',
            message: '删除进度查询失败：' + errMsg,
            collapsed: false,
            steps: this.deleteJobs[filename]?.steps || this.createDeleteSteps(),
          });
          this.stopDeleteJobPolling(filename);
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

    clearDeleteRemovalTimer(filename: string) {
      const timer = this.deleteRemoveTimers[filename];
      if (timer == null) return;
      clearTimeout(timer);
      const { [filename]: _, ...rest } = this.deleteRemoveTimers;
      this.deleteRemoveTimers = rest;
    },

    scheduleDeletedDocumentRemoval(filename: string) {
      this.clearDeleteRemovalTimer(filename);
      const timer = setTimeout(async () => {
        this.documents = this.documents.filter((doc) => doc.filename !== filename);
        const { [filename]: _job, ...jobs } = this.deleteJobs;
        const { [filename]: _timer, ...timers } = this.deleteRemoveTimers;
        this.deleteJobs = jobs;
        this.deleteRemoveTimers = timers;
        try {
          await this.loadDocuments();
        } catch {
          // Catalog 已经撤销该文档；列表刷新失败不应把删除终态改回失败。
        }
      }, 3000);
      this.deleteRemoveTimers = {
        ...this.deleteRemoveTimers,
        [filename]: timer,
      };
    },

    toggleDeleteJobCollapsed(filename: string) {
      const job = this.deleteJobs[filename];
      if (!job) return;
      this.setDeleteJob(filename, { collapsed: !job.collapsed });
    },
  },
});
export type { DocumentItem };
