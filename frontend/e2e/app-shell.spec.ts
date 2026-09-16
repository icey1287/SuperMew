import { expect, test, type Page } from '@playwright/test';
import type { RetrievedChunk } from '../src/types/chat';

async function mockCapabilityCatalog(page: Page, role: 'user' | 'admin' = 'user') {
  const isAdmin = role === 'admin';
  await page.route('**/v1/capabilities', async (route) => {
    await route.fulfill({
      json: {
        schema_version: 1,
        catalog_hash: 'a'.repeat(64),
        skills: [
          {
            name: 'knowledge-base',
            version: '1.0.0',
            description: 'Search the configured knowledge base.',
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
            description: 'Research current public information.',
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
            name: 'sql-assistant',
            version: '1.0.0',
            description: 'Run bounded read-only SQL analysis.',
            activation: '/sql-assistant',
            available: isAdmin,
            availability_reason: isAdmin ? null : 'permission_required',
            required_roles: ['admin'],
            tool_names: ['sql_schema', 'sql_query'],
            approval_tools: [],
            network_policies: ['private-data'],
            resource_scopes: ['private-data-read'],
          },
          {
            name: 'sandbox',
            version: '1.0.0',
            description: 'Execute approved isolated code.',
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
        tools: [],
      },
    });
  });
}

test('renders the unauthenticated application shell and registration mode', async ({ page }) => {
  let releaseRefresh!: () => void;
  const refreshGate = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  await page.route('**/auth/refresh', async (route) => {
    await refreshGate;
    await route.fulfill({ status: 401, json: { detail: 'no refresh session' } });
  });
  await page.goto('/');

  await expect(page).toHaveTitle('喵喵助手 · Knowledge Copilot');
  await expect(page.getByRole('status')).toContainText('正在恢复登录状态');
  await expect(page.getByRole('heading', { name: '登录喵喵助手' })).toBeHidden();
  releaseRefresh();
  await expect(page.getByRole('heading', { name: '登录喵喵助手' })).toBeVisible();
  await expect(page.getByLabel('用户名')).toBeEditable();
  await expect(page.getByLabel('密码')).toBeEditable();

  await page.getByRole('button', { name: '还没有账号？创建一个' }).click();
  await expect(page.getByRole('heading', { name: '注册喵喵助手' })).toBeVisible();
  await expect(page.getByLabel('账号角色')).toBeVisible();
});

test('projects a durable Run event stream into the chat UI', async ({ page }) => {
  const runId = 'run-e2e-1';
  const chunk: RetrievedChunk = {
    filename: 'arkr',
    page_number: 1,
    rrf_rank: 1,
    rerank_score: 0,
    text: `明日方舟中文干员详情（优化版） Page 1。${'这是一段用于检查检索结果换行的中文正文。'.repeat(8)} ${'long_document_identifier_'.repeat(20)}`,
    chunk_id: 'chunk-e2e-1',
    document_id: 'document-e2e-1',
    document_version_id: 'version-e2e-1',
    section_id: 'section-e2e-1',
    index_version: 'index-e2e-1',
    content_hash: 'b'.repeat(64),
  };
  let createRequest: Record<string, unknown> | null = null;
  let createThreadRequest: Record<string, unknown> | null = null;
  let createdThreadId = '';
  let lastEventId = '';

  await mockCapabilityCatalog(page);

  await page.route('**/auth/refresh', async (route) => {
    await route.fulfill({ status: 401, json: { detail: 'no refresh session' } });
  });
  await page.route('**/auth/login', async (route) => {
    await route.fulfill({
      json: { access_token: 'e2e-token', username: 'e2e-user', role: 'user' },
    });
  });
  await page.route('**/v1/threads', async (route) => {
    if (route.request().method() === 'POST') {
      createThreadRequest = route.request().postDataJSON();
      createdThreadId = 'thread_e2e_1';
      await route.fulfill({
        status: 201,
        json: {
          thread_id: createdThreadId,
          title: String(createThreadRequest?.title || '新对话'),
          message_count: 0,
          version: 0,
          thread_status: 'active',
          active_run_id: null,
          active_run_status: null,
          created_at: '2026-07-16T00:00:00Z',
          updated_at: '2026-07-16T00:00:00Z',
        },
      });
      return;
    }
    await route.fulfill({ json: { threads: [] } });
  });
  await page.route('**/v1/threads/*/runs/stream', async (route) => {
    createRequest = route.request().postDataJSON();
    lastEventId = route.request().headers()['last-event-id'] || '';
    const url = new URL(route.request().url());
    expect(decodeURIComponent(url.pathname.split('/')[3])).toBe(createdThreadId);
    const events = [
      ['run.created', { status: 'queued', user_message_id: 101, assistant_message_id: 102 }],
      ['run.started', {}],
      [
        'message.completed',
        {
          content: '这是来自持久化 Run 的回答。',
          status: 'completed',
          rag_trace: {
            initial_retrieved_chunks: [chunk],
            rewrite_retrieved_chunks: [{ ...chunk, filename: '长文档名称'.repeat(30) }],
            retrieved_chunks: [chunk],
          },
        },
      ],
      ['run.completed', {}],
    ];
    const body = events
      .map(([type, data], index) => {
        const event = {
          schema_version: 1,
          event_id: `evt_e2e_${index + 1}`,
          sequence: index + 1,
          run_id: runId,
          thread_id: createdThreadId,
          type,
          timestamp: new Date(2026, 0, 1, 0, 0, index).toISOString(),
          data,
        };
        return `id: ${index + 1}\ndata: ${JSON.stringify(event)}\n\n`;
      })
      .join('');
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: {
        'Cache-Control': 'no-cache',
        'X-Run-ID': runId,
        'X-Thread-Version': '1',
      },
      body,
    });
  });

  await page.goto('/');
  await page.getByLabel('用户名').fill('e2e-user');
  await page.getByLabel('密码').fill('safe-password');
  await page.getByRole('button', { name: '进入工作台' }).click();

  const input = page.getByPlaceholder(/和喵喵说点什么/);
  await expect(input).toBeVisible();
  await input.fill('验证持久化运行');
  await page.getByRole('button', { name: '发送消息' }).click();

  await expect(page.getByText('这是来自持久化 Run 的回答。')).toBeVisible();
  expect(createRequest).toMatchObject({
    message: '验证持久化运行',
    multitask_strategy: 'reject',
    on_disconnect: 'continue',
    approved_tools: [],
  });
  expect(createThreadRequest).toEqual({ title: '验证持久化运行' });
  expect(createdThreadId).toBe('thread_e2e_1');
  expect(lastEventId).toBe('0');
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual(['supermew-theme']);

  await page.locator('.reasoning-details > summary').click();
  await page.locator('.references-title').click();
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const traceCards = page.locator('.reasoning-details .source-item');
    await expect(traceCards).toHaveCount(2);
    for (const card of await traceCards.all()) {
      await expect(card).toContainText('RRF名次：#1');
      await expect(card).toContainText('Rerank分数：0.0000');
      const layout = await card.evaluate((element) => {
        const title = element.querySelector('.source-title-line')!.getBoundingClientRect();
        const meta = element.querySelector('.source-meta-line')!.getBoundingClientRect();
        const excerpt = element.querySelector('.source-excerpt')!.getBoundingClientRect();
        return {
          cardWidth: element.clientWidth,
          excerptWidth: excerpt.width,
          titleBottom: title.bottom,
          metaTop: meta.top,
          metaBottom: meta.bottom,
          excerptTop: excerpt.top,
          overflows: element.scrollWidth > element.clientWidth,
        };
      });
      expect(layout.excerptWidth).toBeGreaterThan(layout.cardWidth * 0.8);
      expect(layout.metaTop).toBeGreaterThanOrEqual(layout.titleBottom);
      expect(layout.excerptTop).toBeGreaterThanOrEqual(layout.metaBottom);
      expect(layout.overflows).toBe(false);
    }
    const reference = page.locator('.references-list .source-item');
    const index = await reference.locator('.ref-index').boundingBox();
    const content = await reference.locator('.source-content').boundingBox();
    expect(index).not.toBeNull();
    expect(content).not.toBeNull();
    expect(content!.x).toBeGreaterThanOrEqual(index!.x + index!.width);
  }
});

test('silently restores an HttpOnly refresh session without rendering the login panel', async ({
  page,
}) => {
  let threadAuthorization = '';
  await mockCapabilityCatalog(page);
  await page.route('**/auth/refresh', async (route) => {
    await route.fulfill({
      json: { access_token: 'restored-token', username: 'restored-user', role: 'user' },
    });
  });
  await page.route('**/v1/threads', async (route) => {
    threadAuthorization = route.request().headers().authorization || '';
    await route.fulfill({ json: { threads: [] } });
  });

  await page.goto('/');

  await expect(page.getByPlaceholder(/和喵喵说点什么/)).toBeVisible();
  await expect(page.getByRole('heading', { name: '登录喵喵助手' })).toBeHidden();
  await expect(page.getByText('restored-user')).toBeVisible();
  await expect.poll(() => threadAuthorization).toBe('Bearer restored-token');
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual(['supermew-theme']);
});

test('returns from history to the chat draft without creating an empty Thread', async ({
  page,
}) => {
  let createThreadCalls = 0;
  await mockCapabilityCatalog(page);
  await page.route('**/auth/refresh', async (route) => {
    await route.fulfill({
      json: { access_token: 'restored-token', username: 'restored-user', role: 'user' },
    });
  });
  await page.route('**/v1/threads', async (route) => {
    if (route.request().method() === 'POST') {
      createThreadCalls += 1;
      await route.fulfill({
        status: 201,
        json: {
          thread_id: 'thread-unexpected-empty',
          title: '新对话',
          message_count: 0,
          version: 0,
          thread_status: 'active',
          active_run_id: null,
          active_run_status: null,
          created_at: '2026-07-17T00:00:00Z',
          updated_at: '2026-07-17T00:00:00Z',
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        threads: [
          {
            thread_id: 'thread-existing',
            title: '已有对话',
            message_count: 2,
            version: 2,
            thread_status: 'active',
            active_run_id: null,
            active_run_status: null,
            created_at: '2026-07-16T00:00:00Z',
            updated_at: '2026-07-17T00:00:00Z',
          },
        ],
      },
    });
  });

  await page.goto('/');
  await expect(page.getByRole('button', { name: '历史对话', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '历史对话', exact: true }).click();
  await expect(page.getByRole('heading', { name: '历史对话' })).toBeVisible();

  await page.getByRole('button', { name: '智能对话', exact: true }).click();

  await expect(page.getByRole('heading', { name: '历史对话' })).toBeHidden();
  await expect(page.getByPlaceholder(/和喵喵说点什么/)).toBeVisible();
  expect(createThreadCalls).toBe(0);
});

test('keeps a failed logout locally revoked across a page reload', async ({ page }) => {
  let refreshCalls = 0;
  let logoutCalls = 0;
  await mockCapabilityCatalog(page);
  await page.route('**/auth/refresh', async (route) => {
    refreshCalls += 1;
    await route.fulfill({
      json: { access_token: 'restored-token', username: 'restored-user', role: 'user' },
    });
  });
  await page.route('**/auth/logout', async (route) => {
    logoutCalls += 1;
    await route.fulfill({ status: 503, json: { detail: 'logout temporarily unavailable' } });
  });
  await page.route('**/v1/threads', async (route) => {
    await route.fulfill({ json: { threads: [] } });
  });

  await page.goto('/');
  await expect(page.getByText('restored-user')).toBeVisible();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '登录喵喵助手' })).toBeVisible();
  await expect.poll(() => logoutCalls).toBe(1);

  await page.reload();

  await expect(page.getByRole('heading', { name: '登录喵喵助手' })).toBeVisible();
  await expect.poll(() => logoutCalls).toBe(2);
  expect(refreshCalls).toBe(1);
  expect(
    await page.evaluate(() => sessionStorage.getItem('supermew-auth-revocation-pending'))
  ).toBe('1');
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual(['supermew-theme']);
});
