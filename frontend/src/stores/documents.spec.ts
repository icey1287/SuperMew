import { createPinia, setActivePinia } from 'pinia';
import { readFileSync } from 'node:fs';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { useDocumentStore } from './documents';
import api from '@/utils/api';

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

const createUploadJob = (overrides: Record<string, any> = {}) => ({
  job_id: 'job_upload_1',
  status: 'running',
  message: '正在向量化入库：450 / 770',
  steps: [
    { key: 'upload', label: '文档上传', percent: 100, status: 'completed', message: '文档上传完成' },
    { key: 'cleanup', label: '清理旧版本', percent: 100, status: 'completed', message: '清理完成' },
    { key: 'parse', label: '解析与分块', percent: 100, status: 'completed', message: '解析完成' },
    { key: 'parent_store', label: '父级分块入库', percent: 100, status: 'completed', message: '父级分块入库完成' },
    { key: 'vector_store', label: '向量化入库', percent: 58, status: 'running', message: '450 / 770' },
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

  it('continues polling upload progress until the active job completes', async () => {
    const store = useDocumentStore();
    const runningJob = createUploadJob();
    const completedJob = createUploadJob({
      status: 'completed',
      message: '文档处理完成',
      steps: [
        ...runningJob.steps.slice(0, 4),
        { key: 'vector_store', label: '向量化入库', percent: 100, status: 'completed', message: '770 / 770' },
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
    expect(store.uploadProgress).toBe('正在向量化入库：450 / 770');
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
});
