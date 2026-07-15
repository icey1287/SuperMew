import { createPinia, setActivePinia } from 'pinia';
import { readFileSync } from 'node:fs';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { useDocumentStore } from './documents';
import api from '@/utils/api';
import type { UploadJob } from '@/types/document';

vi.mock('@/utils/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));

const flushPromises = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

const createUploadJob = (overrides: Partial<UploadJob> = {}): UploadJob => ({
  job_id: 'job_upload_1',
  status: 'running',
  message: '正在写入候选向量：450 / 770',
  steps: [
    { key: 'upload', label: '文档上传', percent: 100, status: 'completed', message: '文档上传完成' },
    { key: 'reserve', label: '候选版本准备', percent: 100, status: 'completed', message: '候选版本已保留' },
    { key: 'parse', label: '解析与版本化分块', percent: 100, status: 'completed', message: '解析完成' },
    { key: 'parent_store', label: '候选父级分块写入', percent: 100, status: 'completed', message: '候选父级分块写入完成' },
    { key: 'vector_store', label: '候选向量写入', percent: 58, status: 'running', message: '450 / 770' },
    { key: 'verify', label: '索引一致性核验', percent: 0, status: 'pending', message: '' },
    { key: 'publish', label: '原子发布新版本', percent: 0, status: 'pending', message: '' },
  ],
  ...overrides,
});

describe('document upload polling', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    const store = useDocumentStore();
    store.stopUploadJobPolling();
    store.stopAllDeleteJobPolling();
    Object.keys(store.deleteRemoveTimers).forEach((filename) =>
      store.clearDeleteRemovalTimer(filename)
    );
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('does not stop upload polling when the settings view unmounts', () => {
    const source = readFileSync(
      new URL('../components/Documents/DocumentSettings.vue', import.meta.url),
      'utf8'
    );
    const unmountedBlock = source.match(/onUnmounted\(\(\) => \{([\s\S]*?)\}\);/);

    expect(unmountedBlock?.[1]).not.toContain('stopUploadJobPolling');
    expect(unmountedBlock?.[1]).toContain('stopAllDeleteJobPolling');
  });

  it('uses the candidate publication pipeline', () => {
    const store = useDocumentStore();

    expect(store.createUploadSteps().map(({ key, label }) => ({ key, label }))).toEqual([
      { key: 'upload', label: '文档上传' },
      { key: 'reserve', label: '候选版本准备' },
      { key: 'parse', label: '解析与版本化分块' },
      { key: 'parent_store', label: '候选父级分块写入' },
      { key: 'vector_store', label: '候选向量写入' },
      { key: 'verify', label: '索引一致性核验' },
      { key: 'publish', label: '原子发布新版本' },
    ]);
  });

  it('continues polling upload progress until the active job completes', async () => {
    const store = useDocumentStore();
    const runningJob = createUploadJob();
    const completedJob = createUploadJob({
      status: 'completed',
      message: '文档处理完成',
      steps: [
        ...runningJob.steps.slice(0, 4),
        { key: 'vector_store', label: '候选向量写入', percent: 100, status: 'completed', message: '770 / 770' },
        { key: 'verify', label: '索引一致性核验', percent: 100, status: 'completed', message: '存储内容一致' },
        { key: 'publish', label: '原子发布新版本', percent: 100, status: 'completed', message: '新版本已发布' },
      ],
    });
    const jobResponses = [runningJob, completedJob];

    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/documents') {
        return Promise.resolve({
          data: {
            documents: [{ filename: 'wuthering-waves.pdf', file_type: 'PDF', chunk_count: 770 }],
          },
        });
      }
      if (url === '/documents/upload/jobs/job_upload_1') {
        return Promise.resolve({ data: jobResponses.shift() || completedJob });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    store.isUploading = true;
    store.selectedFile = { name: 'wuthering-waves.pdf' } as File;

    store.startUploadJobPolling('job_upload_1');
    await flushPromises();

    expect(store.activeUploadJobId).toBe('job_upload_1');
    expect(store.uploadProgress).toBe('正在写入候选向量：450 / 770');
    expect(store.uploadSteps.find((step) => step.key === 'vector_store')).toMatchObject({
      percent: 58,
      status: 'running',
    });
    expect(store.uploadPollTimer).not.toBeNull();

    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();

    expect(store.uploadProgress).toBe('文档处理完成');
    expect(store.uploadSteps.find((step) => step.key === 'vector_store')).toMatchObject({
      percent: 100,
      status: 'completed',
    });
    expect(store.isUploading).toBe(false);
    expect(store.selectedFile).toBeNull();
    expect(store.uploadPollTimer).toBeNull();
    expect(store.documents).toEqual([
      { filename: 'wuthering-waves.pdf', file_type: 'PDF', chunk_count: 770 },
    ]);
  });

  it('keeps polling while a durable job waits to retry', async () => {
    const store = useDocumentStore();
    const retryingJob = createUploadJob({
      status: 'retry_wait',
      message: '候选向量写入暂时失败，等待重试',
    });
    const stagedJob = createUploadJob({
      status: 'staged',
      message: '候选索引已核验，等待原子发布',
    });
    const responses = [retryingJob, stagedJob];

    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/documents/upload/jobs/job_upload_1') {
        return Promise.resolve({ data: responses.shift() || stagedJob });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    store.isUploading = true;
    store.startUploadJobPolling('job_upload_1');
    await flushPromises();

    expect(store.uploadProgress).toBe('候选向量写入暂时失败，等待重试');
    expect(store.uploadPollTimer).not.toBeNull();
    expect(store.isUploading).toBe(true);

    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();

    expect(store.uploadProgress).toBe('候选索引已核验，等待原子发布');
    expect(store.uploadPollTimer).not.toBeNull();
    expect(store.isUploading).toBe(true);
  });

  it.each(['failed', 'cancelled', 'dead_letter'] as const)(
    'stops polling when a durable job reaches %s',
    async (status) => {
      const store = useDocumentStore();
      vi.mocked(api.get).mockResolvedValue({
        data: createUploadJob({ status, message: `任务状态：${status}` }),
      });

      store.isUploading = true;
      store.selectedFile = { name: 'wuthering-waves.pdf' } as File;
      store.startUploadJobPolling('job_upload_1');
      await flushPromises();

      expect(store.uploadProgress).toBe(`任务状态：${status}`);
      expect(store.uploadPollTimer).toBeNull();
      expect(store.isUploading).toBe(false);
      expect(store.selectedFile).not.toBeNull();

      await vi.advanceTimersByTimeAsync(1000);
      expect(api.get).toHaveBeenCalledTimes(1);
    }
  );

  it('keeps the completed upload terminal state when document refresh fails', async () => {
    const store = useDocumentStore();
    const completedJob = createUploadJob({
      status: 'completed',
      message: '文档版本已发布',
    });
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/documents/upload/jobs/job_upload_1') {
        return Promise.resolve({ data: completedJob });
      }
      if (url === '/documents') {
        return Promise.reject(new Error('catalog temporarily unavailable'));
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    store.isUploading = true;
    store.selectedFile = { name: 'guide.pdf' } as File;
    store.startUploadJobPolling('job_upload_1');
    await flushPromises();
    await flushPromises();
    await vi.runAllTimersAsync();
    await flushPromises();

    expect(store.isUploading).toBe(false);
    expect(store.selectedFile).toBeNull();
    expect(store.uploadPollTimer).toBeNull();
    expect(store.uploadProgress).toContain('文档版本已发布');
    expect(store.uploadProgress).toContain('目录刷新失败');
    expect(store.uploadProgress).not.toContain('进度查询失败');
  });

  it('treats cleanup_pending as logically deleted and stops polling', async () => {
    const store = useDocumentStore();
    store.setDeleteJob('guide.pdf', {
      jobId: 'delete-job-1',
      status: 'running',
      message: '正在删除',
      collapsed: false,
      steps: store.createDeleteSteps(),
    });
    vi.mocked(api.get).mockResolvedValue({
      data: {
        job_id: 'delete-job-1',
        status: 'cleanup_pending',
        message: '文档已撤销，物理索引清理待重试',
        steps: store.createDeleteSteps(),
      },
    });

    store.startDeleteJobPolling('guide.pdf', 'delete-job-1');
    await flushPromises();

    expect(store.deleteJobs['guide.pdf'].status).toBe('cleanup_pending');
    expect(store.deletePollTimers['guide.pdf']).toBeUndefined();
    expect(store.deleteRemoveTimers['guide.pdf']).toBeUndefined();
    expect(store.isDeleteActionLocked('guide.pdf')).toBe(false);
  });

  it('serializes delete polls and ignores an older job response', async () => {
    const store = useDocumentStore();
    let resolveFirst: ((value: any) => void) | undefined;
    vi.mocked(api.get).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        })
    );
    store.setDeleteJob('guide.pdf', {
      jobId: 'delete-job-1',
      status: 'running',
      message: '旧任务',
      collapsed: false,
      steps: store.createDeleteSteps(),
    });

    store.startDeleteJobPolling('guide.pdf', 'delete-job-1');
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.get).toHaveBeenCalledTimes(1);

    store.setDeleteJob('guide.pdf', {
      jobId: 'delete-job-2',
      status: 'running',
      message: '新任务',
    });
    resolveFirst?.({
      data: {
        job_id: 'delete-job-1',
        status: 'completed',
        message: '旧任务完成',
        steps: store.createDeleteSteps(),
      },
    });
    await flushPromises();

    expect(store.deleteJobs['guide.pdf'].jobId).toBe('delete-job-2');
    expect(store.deleteJobs['guide.pdf'].message).toBe('新任务');
  });
});
