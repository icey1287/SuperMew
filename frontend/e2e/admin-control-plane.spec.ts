import { expect, test, type Page } from '@playwright/test';

type ModelRole = 'answer' | 'fast' | 'grader' | 'evaluator';

const roles: ModelRole[] = ['answer', 'fast', 'grader', 'evaluator'];

function profile(role: ModelRole, overrides: Record<string, unknown> = {}) {
  return {
    id: `model_${{ answer: 'a', fast: 'b', grader: 'c', evaluator: 'd' }[role].repeat(32)}`,
    display_name: `${role[0].toUpperCase()}${role.slice(1)} Profile`,
    provider: 'openai',
    model_name: `${role}-model-v1`,
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

function initialControlPlane() {
  const profiles = roles.map((role) => profile(role));
  return {
    schema_version: 1,
    catalog_hash: 'a'.repeat(64),
    api_key_configured: true,
    profiles,
    assignments: Object.fromEntries(roles.map((role) => [role, profile(role)])),
    requirements: {
      answer: { supports_stream: true, supports_structured_output: false, temperature: 0.3 },
      fast: { supports_stream: false, supports_structured_output: true, temperature: 0.2 },
      grader: { supports_stream: false, supports_structured_output: true, temperature: 0 },
      evaluator: { supports_stream: false, supports_structured_output: true, temperature: 0 },
    },
  };
}

async function mockAdminShell(page: Page) {
  await page.route('**/auth/refresh', async (route) => {
    await route.fulfill({ status: 401, json: { detail: 'no refresh session' } });
  });
  await page.route('**/auth/login', async (route) => {
    await route.fulfill({
      json: { access_token: 'admin-control-token', username: 'admin-tester', role: 'admin' },
    });
  });
  await page.route('**/v1/capabilities', async (route) => {
    await route.fulfill({
      json: { schema_version: 1, catalog_hash: 'f'.repeat(64), skills: [], tools: [] },
    });
  });
  await page.route('**/v1/threads', async (route) => {
    await route.fulfill({ json: { threads: [] } });
  });
}

async function login(page: Page) {
  await page.goto('/');
  await page.getByLabel('用户名').fill('admin-tester');
  await page.getByLabel('密码').fill('safe-password');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('button', { name: '模型中心' })).toBeVisible();
}

test('creates a Secret-free Model Profile and assigns it to the Evaluator role', async ({
  page,
}) => {
  await mockAdminShell(page);
  const controlPlane = initialControlPlane();
  let createdRequest: Record<string, unknown> | null = null;
  let assignmentRequest: Record<string, unknown> | null = null;

  await page.route(/\/v1\/models(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === 'GET' && path === '/v1/models') {
      await route.fulfill({ json: controlPlane });
      return;
    }
    if (request.method() === 'POST' && path === '/v1/models') {
      createdRequest = request.postDataJSON();
      const created = profile('evaluator', {
        id: `model_${'e'.repeat(32)}`,
        display_name: String(createdRequest?.display_name || ''),
        model_name: String(createdRequest?.model_name || ''),
        base_url: String(createdRequest?.base_url || ''),
        version: 1,
      });
      controlPlane.profiles.push(created);
      controlPlane.catalog_hash = 'e'.repeat(64);
      await route.fulfill({ status: 201, json: controlPlane });
      return;
    }
    if (request.method() === 'PUT' && path === '/v1/models/assignments/evaluator') {
      assignmentRequest = request.postDataJSON();
      const selected = controlPlane.profiles.find(
        (item) => item.id === assignmentRequest?.profile_id
      );
      controlPlane.assignments.evaluator = selected || null;
      controlPlane.catalog_hash = '9'.repeat(64);
      await route.fulfill({ json: controlPlane });
      return;
    }
    await route.fulfill({
      status: 404,
      json: { detail: `unexpected ${request.method()} ${path}` },
    });
  });

  await login(page);
  await page.getByRole('button', { name: '模型中心' }).click();

  await expect(page.getByRole('heading', { name: '模型中心' })).toBeVisible();
  await expect(page.getByText('服务端已配置')).toBeVisible();
  await expect(page.getByText(/仅从 ARK_API_KEY 读取/)).toBeVisible();
  await expect(page.getByLabel(/api key/i)).toHaveCount(0);

  await page.getByRole('button', { name: '新建 Model Profile' }).click();
  const dialog = page.getByRole('dialog', { name: '新建 Model Profile' });
  await dialog.getByLabel('显示名称').fill('Evaluator Profile v2');
  await dialog.getByLabel('模型标识').fill('evaluator-model-v2');
  await dialog.getByLabel('Base URL').fill('https://eval.example.test/v1');
  await dialog.getByRole('button', { name: '创建 Profile' }).click();

  await expect(page.getByText('Evaluator Profile v2', { exact: true })).toBeVisible();
  expect(createdRequest).toMatchObject({
    display_name: 'Evaluator Profile v2',
    model_name: 'evaluator-model-v2',
    base_url: 'https://eval.example.test/v1',
    supports_structured_output: true,
  });
  expect(
    Object.keys(createdRequest || {}).some((key) => key.toLocaleLowerCase().includes('key'))
  ).toBe(false);

  await page.getByLabel('Evaluator 模型').selectOption(`model_${'e'.repeat(32)}`);

  await expect(page.getByText(/已更新 evaluator 模型/)).toBeVisible();
  expect(assignmentRequest).toEqual({ profile_id: `model_${'e'.repeat(32)}` });
});

test('imports a Dataset, runs automated evaluation, and inspects Report plus Case evidence', async ({
  page,
}) => {
  await mockAdminShell(page);
  const controlPlane = initialControlPlane();
  const datasetRecord = {
    id: 'dataset-e2e-1',
    name: 'rag_quality_eval_v1',
    fingerprint: 'b'.repeat(64),
    case_count: 1,
    dataset: {
      schema_version: 1,
      name: 'rag_quality_eval_v1',
      cases: [
        {
          id: 'no-knowledge-001',
          tags: ['smoke', 'routing'],
          critical: true,
          question: '知识库没有覆盖的问题应该如何回答？',
          expected: {
            complexity: 'simple',
            route: 'no_knowledge',
            outcome: 'NO_KNOWLEDGE',
            hitl: 'none',
            acceptable_abstention: true,
          },
        },
      ],
    },
    created_by: 'admin-tester',
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
  };
  const modelSummary = Object.fromEntries(
    roles.map((role) => [
      role,
      {
        profile_id: profile(role).id,
        profile_version: 1,
        display_name: profile(role).display_name,
        provider: 'openai',
        model_name: profile(role).model_name,
        timeout_seconds: 30,
        supports_stream: true,
        supports_structured_output: true,
      },
    ])
  );
  const queuedJob = {
    id: 'evaluation-e2e-1',
    dataset_id: datasetRecord.id,
    dataset_name: datasetRecord.name,
    dataset_fingerprint: datasetRecord.fingerprint,
    baseline_job_id: null,
    status: 'queued',
    completed_cases: 0,
    total_cases: 1,
    progress: 0,
    gate_policy: {
      schema_version: 1,
      k_values: [5, 10],
      critical_no_regression: true,
      required_provenance: 'live_rag',
      metric_gates: [],
    },
    model_catalog_hash: controlPlane.catalog_hash,
    models: modelSummary,
    owner_worker_id: null,
    lease_expires_at: null,
    fencing_token: 0,
    attempts: 0,
    max_attempts: 3,
    error_code: null,
    error: null,
    report: null,
    created_by: 'admin-tester',
    started_at: null,
    finished_at: null,
    created_at: '2026-07-17T00:01:00Z',
    updated_at: '2026-07-17T00:01:00Z',
  };
  const completedJob = {
    ...queuedJob,
    status: 'succeeded',
    completed_cases: 1,
    progress: 1,
    owner_worker_id: 'evaluation-worker-1',
    fencing_token: 1,
    attempts: 1,
    started_at: '2026-07-17T00:01:01Z',
    finished_at: '2026-07-17T00:01:04Z',
    updated_at: '2026-07-17T00:01:04Z',
    report: {
      schema_version: 1,
      dataset_name: datasetRecord.name,
      dataset_fingerprint: datasetRecord.fingerprint,
      case_count: 1,
      observation_count: 1,
      metrics: {
        answer_correctness: { value: 0.96, eligible_cases: 1 },
        groundedness: { value: 0.98, eligible_cases: 1 },
        answer_relevance: { value: 0.94, eligible_cases: 1 },
        completeness: { value: 0.92, eligible_cases: 1 },
        context_relevance: { value: 0.91, eligible_cases: 1 },
        unsupported_claim_rate: { value: 0.02, eligible_cases: 1 },
        conflict_disclosure_rate: { value: 1, eligible_cases: 1 },
      },
      slices: {},
      unavailable_metrics: {},
      cases: [
        {
          case_id: 'no-knowledge-001',
          critical: true,
          metrics: { answer_correctness: 0.96, groundedness: 0.98 },
          checks: { judge_answer_correctness: true },
          provider_failed: false,
          gold_chunk_count: 0,
          matched_gold_chunk_count: 0,
          passed: true,
        },
      ],
      gates: [
        {
          name: 'answer_correctness minimum',
          status: 'passed',
          metric: 'answer_correctness',
          actual: 0.96,
          baseline: null,
          threshold: 0.8,
          detail: 'answer_correctness 达到发布门禁',
        },
      ],
      passed: true,
      metadata: { provenance: 'live_rag' },
    },
  };
  const completedCase = {
    id: 'evaluation-case-e2e-1',
    job_id: completedJob.id,
    case_id: 'no-knowledge-001',
    position: 0,
    status: 'completed',
    question: '知识库没有覆盖的问题应该如何回答？',
    generated_answer: '当前知识库没有足够证据回答这个问题。',
    judge_reason: '答案正确说明证据不足，且没有生成无支持事实。',
    observation: {
      case_id: 'no-knowledge-001',
      complexity: 'simple',
      route: 'no_knowledge',
      outcome: 'NO_KNOWLEDGE',
      hitl: 'none',
      rewrite_performed: false,
      provider_error_code: null,
      duration_ms: 860,
      judge: null,
    },
    judge: {
      answer_correctness: 0.96,
      groundedness: 0.98,
      answer_relevance: 0.94,
      completeness: 0.92,
      context_relevance: 0.91,
      unsupported_claim_rate: 0.02,
      conflict_disclosure_rate: 1,
    },
    metrics: { answer_correctness: 0.96, groundedness: 0.98 },
    checks: { judge_answer_correctness: true },
    retrieved_identities: [
      {
        rank: 1,
        document_id: 'document-public-identity',
        document_version_id: 'version-public-identity',
        chunk_id: 'chunk-public-identity',
        content_sha256: 'c'.repeat(64),
      },
    ],
    provider_error_code: null,
    duration_ms: 860,
    error_code: null,
    error: null,
    started_at: '2026-07-17T00:01:01Z',
    finished_at: '2026-07-17T00:01:04Z',
    created_at: '2026-07-17T00:01:00Z',
    updated_at: '2026-07-17T00:01:04Z',
  };
  let datasetCreated = false;
  let jobCreated = false;
  let datasetCreateRequest: Record<string, unknown> | null = null;
  let jobCreateRequest: Record<string, unknown> | null = null;

  await page.route(/\/v1\/models(?:\?.*)?$/, async (route) => {
    await route.fulfill({ json: controlPlane });
  });
  await page.route(/\/v1\/rag-evaluations(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/v1/rag-evaluations/datasets' && request.method() === 'GET') {
      await route.fulfill({ json: { datasets: datasetCreated ? [datasetRecord] : [] } });
      return;
    }
    if (path === '/v1/rag-evaluations/datasets' && request.method() === 'POST') {
      datasetCreateRequest = request.postDataJSON();
      datasetCreated = true;
      await route.fulfill({ status: 201, json: datasetRecord });
      return;
    }
    if (path === '/v1/rag-evaluations/jobs' && request.method() === 'GET') {
      await route.fulfill({ json: { jobs: jobCreated ? [completedJob] : [] } });
      return;
    }
    if (path === '/v1/rag-evaluations/jobs' && request.method() === 'POST') {
      jobCreateRequest = request.postDataJSON();
      jobCreated = true;
      await route.fulfill({ status: 202, json: queuedJob });
      return;
    }
    if (path === `/v1/rag-evaluations/jobs/${completedJob.id}` && request.method() === 'GET') {
      await route.fulfill({ json: completedJob });
      return;
    }
    if (
      path === `/v1/rag-evaluations/jobs/${completedJob.id}/cases` &&
      request.method() === 'GET'
    ) {
      await route.fulfill({ json: { cases: [completedCase] } });
      return;
    }
    await route.fulfill({
      status: 404,
      json: { detail: `unexpected ${request.method()} ${path}` },
    });
  });

  await login(page);
  await page.getByRole('button', { name: 'RAG 评估' }).click();

  await expect(page.getByRole('heading', { name: 'RAG 评估' })).toBeVisible();
  await expect(page.getByText('四角色模型快照可创建')).toBeVisible();
  await page.getByRole('button', { name: '导入 Dataset' }).click();
  const importDialog = page.getByRole('dialog', { name: '导入 Evaluation Dataset' });
  await importDialog.getByRole('button', { name: '填入示例' }).click();
  await expect(importDialog.getByText('rag_quality_eval_v1', { exact: true })).toBeVisible();
  await importDialog.getByRole('button', { name: '导入 Dataset' }).click();

  expect(datasetCreateRequest).toMatchObject({
    dataset: { schema_version: 1, name: 'rag_quality_eval_v1' },
  });
  await expect(page.getByLabel('Evaluation Dataset', { exact: true })).toHaveValue(
    datasetRecord.id
  );
  await page.getByRole('button', { name: '启动 Evaluation Job' }).click();

  expect(jobCreateRequest).toEqual({ dataset_id: datasetRecord.id, baseline_job_id: null });
  await expect(page.getByText('评估通过')).toBeVisible({ timeout: 8_000 });
  const metricGrid = page.locator('.metric-grid');
  await expect(metricGrid.getByText('Answer correctness', { exact: true })).toBeVisible();
  await expect(metricGrid.getByText('96%', { exact: true })).toBeVisible();
  await expect(page.getByText('answer_correctness 达到发布门禁')).toBeVisible();

  await page.getByRole('tab', { name: /Cases/ }).click();
  await expect(page.getByText('no-knowledge-001')).toBeVisible();
  await page.getByRole('button', { name: '展开 no-knowledge-001' }).click();
  await expect(page.getByText('当前知识库没有足够证据回答这个问题。')).toBeVisible();
  await expect(page.getByText('答案正确说明证据不足，且没有生成无支持事实。')).toBeVisible();
  await expect(page.getByText('document-public-identity')).toBeVisible();
  await expect(page.getByText('chunk-public-identity')).toBeVisible();
});
