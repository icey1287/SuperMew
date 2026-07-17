import { expect, test } from '@playwright/test';

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
  let createRequest: Record<string, unknown> | null = null;
  let createThreadRequest: Record<string, unknown> | null = null;
  let createdThreadId = '';
  let lastEventId = '';

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
  await page.route('**/v1/threads/*/runs', async (route) => {
    createRequest = route.request().postDataJSON();
    const url = new URL(route.request().url());
    expect(decodeURIComponent(url.pathname.split('/')[3])).toBe(createdThreadId);
    await route.fulfill({
      json: {
        run: {
          id: runId,
          thread_id: createdThreadId,
          status: 'queued',
          idempotency_key: String(createRequest?.idempotency_key || ''),
          user_message_id: 101,
          assistant_message_id: 102,
          on_disconnect: 'continue',
        },
        created: true,
        thread_version: 1,
      },
    });
  });
  await page.route(`**/v1/runs/${runId}/stream`, async (route) => {
    lastEventId = route.request().headers()['last-event-id'] || '';
    const events = [
      ['run.created', { status: 'queued', user_message_id: 101, assistant_message_id: 102 }],
      ['run.started', {}],
      ['message.completed', { content: '这是来自持久化 Run 的回答。', status: 'completed' }],
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
      headers: { 'Cache-Control': 'no-cache' },
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
});

test('silently restores an HttpOnly refresh session without rendering the login panel', async ({
  page,
}) => {
  let threadAuthorization = '';
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

test('keeps a failed logout locally revoked across a page reload', async ({ page }) => {
  let refreshCalls = 0;
  let logoutCalls = 0;
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
