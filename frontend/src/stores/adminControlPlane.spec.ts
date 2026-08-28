import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  assignModelRole,
  createModelProfile,
  deleteModelProfile,
  getModelControlPlane,
  updateModelProfile,
} from '@/models/modelClient';
import {
  cancelRagEvaluationJob,
  createRagEvaluationDataset,
  createRagEvaluationJob,
  getRagEvaluationJob,
  listRagEvaluationCases,
  listRagEvaluationDatasets,
  listRagEvaluationJobs,
} from '@/evaluations/evaluationClient';
import type { ModelControlPlane, ModelProfile, ModelRole } from '@/types/models';
import type {
  RagEvaluationCaseResult,
  RagEvaluationDataset,
  RagEvaluationDatasetRecord,
  RagEvaluationJob,
  RagEvaluationJobStatus,
} from '@/types/evaluations';
import { useModelStore } from './models';
import { useEvaluationStore } from './evaluations';

vi.mock('@/models/modelClient', () => ({
  assignModelRole: vi.fn(),
  createModelProfile: vi.fn(),
  deleteModelProfile: vi.fn(),
  getModelControlPlane: vi.fn(),
  updateModelProfile: vi.fn(),
}));

vi.mock('@/evaluations/evaluationClient', () => ({
  cancelRagEvaluationJob: vi.fn(),
  createRagEvaluationDataset: vi.fn(),
  createRagEvaluationJob: vi.fn(),
  getRagEvaluationJob: vi.fn(),
  listRagEvaluationCases: vi.fn(),
  listRagEvaluationDatasets: vi.fn(),
  listRagEvaluationJobs: vi.fn(),
}));

const roles: ModelRole[] = ['answer', 'fast', 'grader', 'evaluator'];

function profile(role: ModelRole, overrides: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: `model_${role.padEnd(32, '0')}`,
    display_name: `${role} profile`,
    provider: 'openai',
    model_name: `${role}-model`,
    base_url: 'https://models.example.test/v1',
    timeout_seconds: 30,
    supports_stream: true,
    supports_structured_output: true,
    enabled: true,
    source: 'user',
    version: 1,
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
    ...overrides,
  };
}

function controlPlane(overrides: Partial<ModelControlPlane> = {}): ModelControlPlane {
  const assignments = Object.fromEntries(roles.map((role) => [role, profile(role)])) as Record<
    ModelRole,
    ModelProfile
  >;
  return {
    schema_version: 1,
    catalog_hash: 'a'.repeat(64),
    api_key_configured: true,
    profiles: Object.values(assignments),
    assignments,
    requirements: {
      answer: { supports_stream: true, supports_structured_output: false, temperature: 0.3 },
      fast: { supports_stream: false, supports_structured_output: true, temperature: 0.2 },
      grader: { supports_stream: false, supports_structured_output: true, temperature: 0 },
      evaluator: { supports_stream: false, supports_structured_output: true, temperature: 0 },
    },
    ...overrides,
  };
}

const dataset: RagEvaluationDataset = {
  schema_version: 1,
  name: 'rag_eval_v1',
  cases: [
    {
      id: 'case-1',
      question: '知识库之外的问题',
      expected: {
        route: 'no_knowledge',
        outcome: 'NO_KNOWLEDGE',
        acceptable_abstention: true,
      },
    },
  ],
};

function datasetRecord(): RagEvaluationDatasetRecord {
  return {
    id: 'dataset-1',
    name: dataset.name,
    fingerprint: 'b'.repeat(64),
    case_count: dataset.cases.length,
    dataset,
    created_by: 'admin',
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
  };
}

function job(
  status: RagEvaluationJobStatus = 'succeeded',
  overrides: Partial<RagEvaluationJob> = {}
): RagEvaluationJob {
  const completed = status === 'succeeded' ? 1 : 0;
  return {
    id: `evaluation-${status}`,
    dataset_id: 'dataset-1',
    dataset_name: dataset.name,
    dataset_fingerprint: 'b'.repeat(64),
    baseline_job_id: null,
    status,
    completed_cases: completed,
    total_cases: 1,
    progress: completed,
    gate_policy: { schema_version: 1, required_provenance: 'live_rag' },
    model_catalog_hash: 'a'.repeat(64),
    models: Object.fromEntries(
      roles.map((role) => [
        role,
        {
          profile_id: profile(role).id,
          profile_version: 1,
          display_name: `${role} profile`,
          provider: 'openai',
          model_name: `${role}-model`,
          timeout_seconds: 30,
          supports_stream: true,
          supports_structured_output: true,
        },
      ])
    ),
    owner_worker_id: status === 'running' ? 'worker-1' : null,
    lease_expires_at: null,
    fencing_token: 1,
    attempts: status === 'queued' ? 0 : 1,
    max_attempts: 3,
    error_code: null,
    error: null,
    report:
      status === 'succeeded'
        ? {
            schema_version: 1,
            dataset_name: dataset.name,
            dataset_fingerprint: 'b'.repeat(64),
            case_count: 1,
            observation_count: 1,
            metrics: { answer_correctness: { value: 0.9, eligible_cases: 1 } },
            slices: {},
            unavailable_metrics: {},
            cases: [
              {
                case_id: 'case-1',
                critical: false,
                metrics: { answer_correctness: 0.9 },
                checks: { judge_answer_correctness: true },
                provider_failed: false,
                gold_chunk_count: 0,
                matched_gold_chunk_count: 0,
                passed: true,
              },
            ],
            gates: [],
            passed: true,
            metadata: {},
          }
        : null,
    created_by: 'admin',
    started_at: status === 'queued' ? null : '2026-07-17T00:01:00Z',
    finished_at: status === 'succeeded' ? '2026-07-17T00:02:00Z' : null,
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:02:00Z',
    ...overrides,
  };
}

function caseResult(): RagEvaluationCaseResult {
  return {
    id: 'evaluation-case-1',
    job_id: 'evaluation-succeeded',
    case_id: 'case-1',
    position: 0,
    status: 'completed',
    question: dataset.cases[0].question,
    generated_answer: '知识库暂无相关资料。',
    judge_reason: '正确披露无知识。',
    observation: {
      case_id: 'case-1',
      route: 'no_knowledge',
      outcome: 'NO_KNOWLEDGE',
      hitl: 'none',
      duration_ms: 120,
    },
    judge: null,
    metrics: { answer_correctness: 0.9 },
    checks: { judge_answer_correctness: true },
    retrieved_identities: [],
    provider_error_code: null,
    duration_ms: 120,
    error_code: null,
    error: null,
    started_at: '2026-07-17T00:01:00Z',
    finished_at: '2026-07-17T00:01:01Z',
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:01:01Z',
  };
}

describe('admin control-plane stores', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    useEvaluationStore().stopPolling();
    vi.useRealTimers();
  });

  it('hydrates the Model Profile catalog and requires Secret plus all four Assignments', async () => {
    vi.mocked(getModelControlPlane).mockResolvedValue(controlPlane());
    const store = useModelStore();

    await store.fetchControlPlane();

    expect(store.profiles).toHaveLength(4);
    expect(store.readyForEvaluation).toBe(true);

    store.controlPlane = controlPlane({ api_key_configured: false });
    expect(store.readyForEvaluation).toBe(false);
  });

  it('updates role Assignments through one mutation interface and never handles an API Key', async () => {
    const next = controlPlane();
    const replacement = profile('evaluator', {
      id: `model_${'f'.repeat(32)}`,
      display_name: 'evaluation v2',
    });
    next.profiles.push(replacement);
    next.assignments.evaluator = replacement;
    vi.mocked(assignModelRole).mockResolvedValue(next);
    const store = useModelStore();
    store.controlPlane = controlPlane();

    await store.assignRole('evaluator', replacement.id);

    expect(assignModelRole).toHaveBeenCalledWith('evaluator', { profile_id: replacement.id });
    expect(store.assignments?.evaluator?.display_name).toBe('evaluation v2');
    expect(store.controlPlane?.api_key_configured).toBe(true);
    expect(store.controlPlane).not.toHaveProperty('api_key');
    expect(JSON.stringify(store.controlPlane).toLocaleLowerCase()).not.toContain('secret_value');
  });

  it('restores Dataset, Job, Case and Report projections through one initialization seam', async () => {
    const completedJob = job();
    vi.mocked(listRagEvaluationDatasets).mockResolvedValue([datasetRecord()]);
    vi.mocked(listRagEvaluationJobs).mockResolvedValue([completedJob]);
    vi.mocked(getRagEvaluationJob).mockResolvedValue(completedJob);
    vi.mocked(listRagEvaluationCases).mockResolvedValue([caseResult()]);
    const store = useEvaluationStore();

    await store.initialize();

    expect(store.selectedJobId).toBe(completedJob.id);
    expect(store.selectedJob?.report?.metrics.answer_correctness.value).toBe(0.9);
    expect(store.selectedCases[0].question).toBe(dataset.cases[0].question);
    expect(store.pollTimer).toBeNull();
  });

  it('creates durable Dataset and Job projections, polls active work, and applies cancellation', async () => {
    const record = datasetRecord();
    const queued = job('queued');
    const cancelled = job('cancelled', { id: queued.id });
    vi.mocked(createRagEvaluationDataset).mockResolvedValue(record);
    vi.mocked(createRagEvaluationJob).mockResolvedValue(queued);
    vi.mocked(cancelRagEvaluationJob).mockResolvedValue(cancelled);
    const store = useEvaluationStore();

    await store.createDataset(dataset);
    await store.createJob({ dataset_id: record.id });

    expect(store.datasets[0]).toEqual(record);
    expect(store.selectedJobId).toBe(queued.id);
    expect(store.pollTimer).not.toBeNull();

    await store.cancelJob(queued.id);

    expect(cancelRagEvaluationJob).toHaveBeenCalledWith(queued.id);
    expect(store.selectedJob?.status).toBe('cancelled');
  });

  it('keeps CRUD calls in the Model Store while preserving server-returned catalog state', async () => {
    const created = controlPlane();
    vi.mocked(createModelProfile).mockResolvedValue(created);
    vi.mocked(updateModelProfile).mockResolvedValue(created);
    vi.mocked(deleteModelProfile).mockResolvedValue(undefined);
    vi.mocked(getModelControlPlane).mockResolvedValue(created);
    const store = useModelStore();
    const payload = {
      display_name: 'new profile',
      provider: 'openai' as const,
      model_name: 'new-model',
      base_url: '',
      timeout_seconds: 30,
      supports_stream: true,
      supports_structured_output: true,
      enabled: true,
    };

    await store.createProfile(payload);
    await store.updateProfile(profile('answer').id, payload);
    await store.deleteProfile(profile('answer').id);

    expect(createModelProfile).toHaveBeenCalledWith(payload);
    expect(updateModelProfile).toHaveBeenCalledWith(profile('answer').id, payload);
    expect(deleteModelProfile).toHaveBeenCalledWith(profile('answer').id);
    expect(store.controlPlane).toEqual(created);
  });
});
