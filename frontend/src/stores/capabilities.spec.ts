import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getCapabilityCatalog } from '@/capabilities/capabilityClient';
import type {
  CapabilityCatalogResponse,
  CapabilitySkill,
  CapabilityTool,
} from '@/types/capabilities';
import { useCapabilityStore } from './capabilities';

vi.mock('@/capabilities/capabilityClient', () => ({
  getCapabilityCatalog: vi.fn(),
}));

const tools: CapabilityTool[] = [
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
    name: 'sandbox_execute',
    description: 'Execute isolated code.',
    group: 'sandbox-execution',
    version: '1.0.0',
    exposure: 'deferred',
    available: true,
    availability_reason: null,
    required_roles: ['admin'],
    requires_approval: true,
    network_policy: 'none',
    resource_scope: 'code-execution',
    idempotent: false,
  },
];

const skills: CapabilitySkill[] = [
  {
    name: 'web-research',
    version: '1.0.0',
    description: 'Research current public information.',
    activation: '/web-research',
    available: true,
    availability_reason: null,
    required_roles: [],
    tool_names: ['web_search'],
    approval_tools: [],
    network_policies: ['restricted'],
    resource_scopes: ['public-web'],
  },
  {
    name: 'sql-assistant',
    version: '1.0.0',
    description: 'Run bounded read-only SQL.',
    activation: '/sql-assistant',
    available: false,
    availability_reason: 'permission_required',
    required_roles: ['admin'],
    tool_names: ['sql_query', 'sql_schema'],
    approval_tools: [],
    network_policies: ['private-data'],
    resource_scopes: ['private-data-read'],
  },
  {
    name: 'sandbox',
    version: '1.0.0',
    description: 'Execute approved isolated code.',
    activation: '/sandbox',
    available: true,
    availability_reason: null,
    required_roles: ['admin'],
    tool_names: ['sandbox_execute'],
    approval_tools: ['sandbox_execute'],
    network_policies: ['none'],
    resource_scopes: ['code-execution'],
  },
];

function catalog(overrides: Partial<CapabilityCatalogResponse> = {}): CapabilityCatalogResponse {
  return {
    schema_version: 1,
    catalog_hash: 'a'.repeat(64),
    skills,
    tools,
    ...overrides,
  };
}

describe('capability store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('covers loading, successful fetch, and the empty catalog state', async () => {
    let finish!: (value: CapabilityCatalogResponse) => void;
    vi.mocked(getCapabilityCatalog).mockImplementation(
      () => new Promise((resolve) => (finish = resolve))
    );
    const store = useCapabilityStore();

    const request = store.fetchCatalog();
    expect(store.loading).toBe(true);
    expect(store.error).toBe('');

    finish(catalog({ skills: [], tools: [] }));
    await request;

    expect(store.loading).toBe(false);
    expect(store.isEmpty).toBe(true);
  });

  it('normalizes fetch errors and retries through the same interface', async () => {
    vi.mocked(getCapabilityCatalog)
      .mockRejectedValueOnce(new TypeError('private transport detail'))
      .mockResolvedValueOnce(catalog());
    const store = useCapabilityStore();

    await expect(store.fetchCatalog()).rejects.toMatchObject({
      code: 'NETWORK_UNAVAILABLE',
    });
    expect(store.error).toBe('无法连接服务，请检查网络后重试');

    await expect(store.retryCatalog()).resolves.toMatchObject({ schema_version: 1 });
    expect(store.error).toBe('');
    expect(store.skills).toHaveLength(3);
  });

  it('keeps the center and command palette mutually exclusive', () => {
    const store = useCapabilityStore();

    store.openCenter();
    expect(store.centerOpen).toBe(true);
    expect(store.paletteOpen).toBe(false);

    store.openPalette();
    expect(store.paletteOpen).toBe(true);
    expect(store.centerOpen).toBe(false);

    store.togglePalette();
    expect(store.paletteOpen).toBe(false);
  });

  it('searches and filters the Skill catalog without exposing hidden schemas', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();

    store.setSearchQuery('sql_query');
    expect(store.filteredSkills.map((skill) => skill.name)).toEqual(['sql-assistant']);

    store.setAvailabilityFilter('available');
    expect(store.filteredSkills).toEqual([]);

    store.setSearchQuery('');
    expect(store.filteredSkills.map((skill) => skill.name)).toEqual(['web-research', 'sandbox']);
  });

  it('fails closed when a missing or unavailable Skill is selected', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();

    expect(() => store.selectSkill('sql-assistant')).toThrowError(
      '所选能力当前不可用，不能启动 Run。'
    );
    expect(store.selectedSkillName).toBeNull();

    expect(() => store.selectSkill('removed-skill')).toThrowError('所选能力不存在或已被移除。');
  });

  it('remembers the selected mode independently for each Thread', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();

    store.setActiveThread('thread-1');
    store.selectSkill('web-research');
    store.setActiveThread('thread-2');
    expect(store.selectedSkillName).toBeNull();
    store.selectSkill('sandbox');

    store.setActiveThread('thread-1');
    expect(store.selectedSkillName).toBe('web-research');
    store.setActiveThread('thread-2');
    expect(store.selectedSkillName).toBe('sandbox');
    expect(store.pendingApprovalDraft).toMatchObject({
      skillName: 'sandbox',
      toolNames: ['sandbox_execute'],
      confirmed: false,
    });
  });

  it('restores a durable historical Skill without authorizing an unavailable rerun', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();
    store.setActiveThread('thread-history');

    store.restoreThreadSkill('sql-assistant', 'thread-history');

    expect(store.selectedSkillName).toBe('sql-assistant');
    expect(store.selectedModeUnavailableReason).toBe('permission_required');
    expect(store.pendingApprovalDraft).toBeNull();
    expect(() => store.composeExecutionMessage('再次运行')).toThrowError(
      '所选能力当前不可用，不能启动 Run。'
    );
  });

  it('composes general and slash-activated messages with a strict first column', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();

    expect(store.composeExecutionMessage('  普通问题  ')).toEqual({
      message: '普通问题',
      approvedTools: [],
    });

    store.selectSkill('web-research');
    expect(store.composeExecutionMessage('调查今天的发布信息')).toEqual({
      message: '/web-research\n调查今天的发布信息',
      approvedTools: [],
    });
    expect(store.composeExecutionMessage('调查今天的发布信息').message.startsWith('/')).toBe(true);
  });

  it('activates an available SQL Assistant without requesting write approval', () => {
    const store = useCapabilityStore();
    store.catalog = catalog({
      skills: skills.map((skill) =>
        skill.name === 'sql-assistant'
          ? { ...skill, available: true, availability_reason: null }
          : skill
      ),
    });

    store.selectSkill('sql-assistant');

    expect(store.composeExecutionMessage('分析最近 30 天核心指标')).toEqual({
      message: '/sql-assistant\n分析最近 30 天核心指标',
      approvedTools: [],
    });
    expect(store.pendingApprovalDraft).toBeNull();
  });

  it('requires an explicit approval before composing Sandbox execution', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();
    store.selectSkill('sandbox');
    store.setSandboxLanguage('sh');

    expect(() => store.composeExecutionMessage('printf "ok"')).toThrowError(
      '该能力需要先确认高风险工具审批。'
    );

    store.openApproval();
    expect(store.approvalOpen).toBe(true);
    store.confirmPendingApproval();
    expect(store.approvalOpen).toBe(false);
    const composed = store.composeExecutionMessage('printf "ok"');
    expect(composed.approvedTools).toEqual(['sandbox_execute']);
    expect(composed.message).toBe(
      '/sandbox\n请调用 sandbox_execute，并严格使用以下 JSON 参数执行隔离代码：\n' +
        '{\n  "language": "sh",\n  "source": "printf \\"ok\\""\n}'
    );
  });

  it('never accepts approval Tool names outside the selected Skill declaration', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();
    store.selectSkill('sandbox');
    store.pendingApprovalDraft = {
      skillName: 'sandbox',
      toolNames: ['sandbox_execute', 'sql_query'],
      confirmed: false,
    };

    expect(() => store.confirmPendingApproval()).toThrowError('当前没有可确认的工具审批。');
    expect(store.selectedApprovedTools).toEqual([]);
  });

  it('surfaces a previously selected mode becoming unavailable after refresh', async () => {
    const store = useCapabilityStore();
    store.catalog = catalog();
    store.selectSkill('web-research');
    vi.mocked(getCapabilityCatalog).mockResolvedValue(
      catalog({
        skills: skills.map((skill) =>
          skill.name === 'web-research'
            ? { ...skill, available: false, availability_reason: 'not_configured' }
            : skill
        ),
      })
    );

    await store.fetchCatalog();

    expect(store.selectedSkillName).toBe('web-research');
    expect(store.selectedModeUnavailableReason).toBe('not_configured');
    expect(() => store.composeExecutionMessage('继续研究')).toThrowError(
      '所选能力当前不可用，不能启动 Run。'
    );
  });

  it('resets all authentication-scoped capability state', () => {
    const store = useCapabilityStore();
    store.catalog = catalog();
    store.openCenter();
    store.setActiveThread('thread-1');
    store.selectSkill('web-research');

    store.reset();

    expect(store.catalog).toBeNull();
    expect(store.centerOpen).toBe(false);
    expect(store.selectedSkillName).toBeNull();
    expect(store.selectedSkillByThread).toEqual({});
  });
});
