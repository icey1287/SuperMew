import { expect, test, type Page } from '@playwright/test';

function capabilityCatalog(role: 'user' | 'admin') {
  const isAdmin = role === 'admin';
  const tools = [
    {
      name: 'web_search',
      description: 'Search public web evidence.',
      group: 'web-research',
      version: '1.0.0',
      exposure: 'deferred',
      available: true,
      availability_reason: null,
      required_roles: [],
      requires_approval: false,
      network_policy: 'restricted',
      resource_scope: 'public-web',
      idempotent: true,
    },
    {
      name: 'sql_query',
      description: 'Execute bounded read-only SQL.',
      group: 'sql',
      version: '1.0.0',
      exposure: 'deferred',
      available: isAdmin,
      availability_reason: isAdmin ? null : 'permission_required',
      required_roles: ['admin'],
      requires_approval: false,
      network_policy: 'private-data',
      resource_scope: 'private-data-read',
      idempotent: true,
    },
    {
      name: 'sandbox_execute',
      description: 'Execute code in the isolated Sandbox.',
      group: 'sandbox-execution',
      version: '1.0.0',
      exposure: 'deferred',
      available: isAdmin,
      availability_reason: isAdmin ? null : 'permission_required',
      required_roles: ['admin'],
      requires_approval: true,
      network_policy: 'none',
      resource_scope: 'code-execution',
      idempotent: false,
    },
  ];
  return {
    schema_version: 1,
    catalog_hash: 'b'.repeat(64),
    skills: [
      {
        name: 'knowledge-base',
        version: '1.0.0',
        description: 'Answer from uploaded documents with citations.',
        activation: '/knowledge-base',
        available: true,
        availability_reason: null,
        required_roles: [],
        tool_names: ['search_knowledge_base'],
        approval_tools: [],
        network_policies: ['restricted'],
        resource_scopes: ['knowledge-read'],
      },
      {
        name: 'web-research',
        version: '1.0.0',
        description: 'Research current public information with citations.',
        activation: '/web-research',
        available: true,
        availability_reason: null,
        required_roles: [],
        tool_names: ['web_search', 'web_fetch'],
        approval_tools: [],
        network_policies: ['restricted'],
        resource_scopes: ['public-web'],
      },
      {
        name: 'weather',
        version: '1.0.0',
        description:
          'Query structured current weather or forecast data for a Chinese city，查询天气、气温、湿度、风向、实况与预报；工具不可用时明确说明且不猜测。',
        activation: '/weather',
        available: true,
        availability_reason: null,
        required_roles: [],
        tool_names: [],
        approval_tools: [],
        network_policies: ['restricted'],
        resource_scopes: ['public-web'],
      },
      {
        name: 'sql-assistant',
        version: '1.0.0',
        description: 'Analyze authorized data with bounded read-only SQL.',
        activation: '/sql-assistant',
        available: isAdmin,
        availability_reason: isAdmin ? null : 'permission_required',
        required_roles: ['admin'],
        tool_names: ['sql_query'],
        approval_tools: [],
        network_policies: ['private-data'],
        resource_scopes: ['private-data-read'],
      },
      {
        name: 'sandbox',
        version: '1.0.0',
        description: 'Execute approved code in an isolated workspace.',
        activation: '/sandbox',
        available: isAdmin,
        availability_reason: isAdmin ? null : 'permission_required',
        required_roles: ['admin'],
        tool_names: ['sandbox_execute'],
        approval_tools: ['sandbox_execute'],
        network_policies: ['none'],
        resource_scopes: ['code-execution'],
      },
    ],
    tools,
  };
}

async function mockAuthenticatedShell(page: Page, role: 'user' | 'admin') {
  let threadCounter = 0;
  await page.route('**/auth/refresh', async (route) => {
    await route.fulfill({ status: 401, json: { detail: 'no refresh session' } });
  });
  await page.route('**/auth/login', async (route) => {
    await route.fulfill({
      json: { access_token: 'capability-token', username: `${role}-tester`, role },
    });
  });
  await page.route('**/v1/capabilities', async (route) => {
    await route.fulfill({ json: capabilityCatalog(role) });
  });
  await page.route('**/v1/threads', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: { threads: [] } });
      return;
    }
    threadCounter += 1;
    const request = route.request().postDataJSON() as { title?: string };
    await route.fulfill({
      status: 201,
      json: {
        thread_id: `thread_capability_${threadCounter}`,
        title: request.title || '新对话',
        message_count: 0,
        version: 0,
        thread_status: 'active',
        active_run_id: null,
        active_run_status: null,
        created_at: '2026-07-17T00:00:00Z',
        updated_at: '2026-07-17T00:00:00Z',
      },
    });
  });
}

async function login(page: Page, role: 'user' | 'admin') {
  await page.goto('/');
  await page.getByLabel('用户名').fill(`${role}-tester`);
  await page.getByLabel('密码').fill('safe-password');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('button', { name: '选择运行模式' })).toBeVisible();
}

function sseBody(runId: string, threadId: string, events: Array<[string, object]>) {
  return events
    .map(([type, data], index) => {
      const sequence = index + 1;
      return `id: ${sequence}\ndata: ${JSON.stringify({
        schema_version: 1,
        event_id: `evt_capability_${sequence}`,
        sequence,
        run_id: runId,
        thread_id: threadId,
        type,
        timestamp: `2026-07-17T00:00:0${sequence}Z`,
        data,
      })}\n\n`;
    })
    .join('');
}

test('shows concise Chinese skill copy without repeating the selected mode', async ({ page }) => {
  await mockAuthenticatedShell(page, 'admin');
  await login(page, 'admin');

  await page.keyboard.press('Control+K');
  const palette = page.getByRole('dialog', { name: '能力命令面板' });
  const weatherOption = palette.getByRole('option', { name: /天气查询/ });
  await expect(weatherOption).toContainText('查询天气、气温和未来预报');
  await expect(weatherOption).not.toContainText('Query structured');
  await weatherOption.click();

  const inputArea = page.locator('.input-area-wrapper');
  await expect(inputArea.getByText('天气查询', { exact: true })).toHaveCount(1);
  await expect(inputArea.getByText('查询天气、气温和未来预报', { exact: true })).toHaveCount(1);
  await expect(inputArea.locator('.mode-composer-panel')).toHaveCount(0);
});

test('discovers Skills, enters Web Research, and renders Tool timeline plus Artifact', async ({
  page,
}) => {
  await mockAuthenticatedShell(page, 'user');
  let createRequest: Record<string, unknown> | null = null;
  const runId = 'run_web_product';

  await page.route('**/v1/threads/*/runs/stream', async (route) => {
    createRequest = route.request().postDataJSON();
    const threadId = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: {
        'X-Run-ID': runId,
        'X-Thread-Version': '1',
      },
      body: sseBody(runId, threadId, [
        ['run.created', { status: 'queued', user_message_id: 201, assistant_message_id: 202 }],
        ['run.started', {}],
        ['tool.started', { tool_name: 'web_search', tool_call_id: 'call_web' }],
        [
          'tool.completed',
          {
            tool_name: 'web_search',
            tool_call_id: 'call_web',
            duration_ms: 180,
            result_size: 2048,
            guardrail_decision: 'ALLOW',
            reason_code: 'ALLOWED',
          },
        ],
        [
          'artifact.created',
          {
            artifact_id: 'art_sources',
            name: 'sources.json',
            media_type: 'application/json',
            uri: '/api/artifacts/art_sources',
            size_bytes: 2048,
            tool_name: 'web_search',
            tool_call_id: 'call_web',
          },
        ],
        ['message.completed', { content: '公开信息核验完成。', status: 'completed' }],
        ['run.completed', {}],
      ]),
    });
  });
  await page.route('**/api/artifacts/art_sources', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ sources: ['https://example.com/release'] }),
    });
  });

  await login(page, 'user');
  await page.getByRole('button', { name: '能力中心' }).click();
  const center = page.getByRole('dialog', { name: '能力中心' });
  await expect(center).toBeVisible();
  const sqlCard = center.locator('article').filter({ hasText: '数据分析' });
  await expect(sqlCard.getByText('权限不足')).toBeVisible();
  await expect(sqlCard.getByText('角色：admin')).toBeVisible();
  await expect(sqlCard.getByRole('button', { name: '使用' })).toBeDisabled();
  const webCard = center.locator('article').filter({ hasText: '网页调研' });
  await webCard.getByRole('button', { name: '使用' }).click();

  const input = page.getByPlaceholder(/描述要调研的公开问题/);
  await input.fill('核验今天的公开发布信息');
  await page.getByRole('button', { name: '发送消息' }).click();

  await expect(page.getByText('公开信息核验完成。')).toBeVisible();
  const inspector = page.locator('.knowledge-context');
  await expect(inspector.getByText('web_search执行完成')).toBeVisible();
  await expect(inspector.locator('.execution-node.is-running')).toHaveCount(0);
  await expect(
    inspector.locator('.execution-item').filter({ hasText: '开始执行' }).locator('.execution-node')
  ).toHaveClass(/is-completed/);
  await expect(inspector.getByText('策略允许')).toHaveCount(0);
  await expect(
    inspector.getByRole('region', { name: 'Artifacts' }).getByText('sources.json', { exact: true })
  ).toBeVisible();
  const artifactTrigger = inspector.getByRole('button', { name: '预览 sources.json' });
  await artifactTrigger.click();
  const artifactPreview = page.getByRole('dialog', { name: '预览 sources.json' });
  const closePreview = artifactPreview.getByRole('button', { name: '关闭 Artifact 预览' });
  await expect(artifactPreview).toBeVisible();
  await expect(closePreview).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(closePreview).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(closePreview).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(artifactPreview).toBeHidden();
  await expect(artifactTrigger).toBeFocused();
  await expect(
    page.locator('.user-message .message-content').getByText('核验今天的公开发布信息', {
      exact: true,
    })
  ).toBeVisible();
  expect(createRequest).toMatchObject({
    message: '/web-research\n核验今天的公开发布信息',
    approved_tools: [],
  });
});

test('opens the command palette and binds Sandbox approval to the created Run', async ({
  page,
}) => {
  await mockAuthenticatedShell(page, 'admin');
  let createRequest: Record<string, unknown> | null = null;
  const runId = 'run_sandbox_product';

  await page.route('**/v1/threads/*/runs/stream', async (route) => {
    createRequest = route.request().postDataJSON();
    const threadId = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: {
        'X-Run-ID': runId,
        'X-Thread-Version': '1',
      },
      body: sseBody(runId, threadId, [
        ['run.created', { status: 'queued', user_message_id: 301, assistant_message_id: 302 }],
        ['run.started', {}],
        ['tool.started', { tool_name: 'sandbox_execute', tool_call_id: 'call_sandbox' }],
        [
          'tool.completed',
          {
            tool_name: 'sandbox_execute',
            tool_call_id: 'call_sandbox',
            duration_ms: 80,
            guardrail_decision: 'ALLOW',
            reason_code: 'ALLOWED',
          },
        ],
        ['message.completed', { content: 'Sandbox 输出：42', status: 'completed' }],
        ['run.completed', {}],
      ]),
    });
  });

  await login(page, 'admin');
  await page.keyboard.press('Control+K');
  const palette = page.getByRole('dialog', { name: '能力命令面板' });
  await expect(palette).toBeVisible();
  await palette.getByRole('combobox').fill('代码沙盒');
  await palette.getByRole('option', { name: /代码沙盒/ }).click();

  const input = page.getByPlaceholder(/输入要在隔离环境中执行的代码/);
  await input.fill('print(6 * 7)');
  await page.getByRole('button', { name: '发送消息' }).click();

  const approval = page.getByRole('dialog', { name: '确认高风险工具授权' });
  await expect(approval).toBeVisible();
  await expect(approval.getByText('sandbox_execute')).toBeVisible();
  await expect(approval.getByText(/仅用于当前对话中即将发起的一次任务/)).toBeVisible();
  expect(createRequest).toBeNull();

  await approval.getByRole('button', { name: '确认并发送' }).click();
  await expect(page.getByText('Sandbox 输出：42')).toBeVisible();
  expect(createRequest).toMatchObject({ approved_tools: ['sandbox_execute'] });
  expect(String(createRequest?.message)).toContain('/sandbox\n');
  expect(String(createRequest?.message)).toContain('"language": "python"');
  expect(String(createRequest?.message)).toContain('"source": "print(6 * 7)"');
});

test('keeps the live Tool timeline reachable on a narrow screen before message output', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1181, height: 900 });
  await mockAuthenticatedShell(page, 'user');
  const runId = 'run_web_mobile_live';

  await page.route('**/v1/threads/*/runs/stream', async (route) => {
    const threadId = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: {
        'X-Run-ID': runId,
        'X-Thread-Version': '1',
      },
      body: sseBody(runId, threadId, [
        ['run.created', { status: 'queued', user_message_id: 401, assistant_message_id: 402 }],
        ['run.started', {}],
        ['tool.started', { tool_name: 'web_search', tool_call_id: 'call_mobile_web' }],
      ]),
    });
  });

  await login(page, 'user');
  await page
    .locator('.welcome-mode-grid')
    .getByRole('button', { name: /网页调研/ })
    .click();
  await page.getByPlaceholder(/描述要调研的公开问题/).fill('持续观察公开发布');
  await page.getByRole('button', { name: '发送消息' }).click();

  const liveInspector = page.locator('.message-run-inspector.is-pre-delta');
  await expect(page.locator('.knowledge-context')).toBeVisible();
  await expect(liveInspector).toBeHidden();

  for (const width of [1180, 720, 480]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.locator('.knowledge-context')).toBeHidden();
    await expect(liveInspector).toBeVisible();
    await expect(liveInspector.getByText('web_search开始执行')).toBeVisible();
  }
  await expect(page.locator('.bot-message .thinking-content')).toBeVisible();
});
