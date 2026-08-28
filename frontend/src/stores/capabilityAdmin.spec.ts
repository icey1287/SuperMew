import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createManagedSkill,
  deleteManagedTool,
  getCapabilityControlPlane,
  updateSqlAssistantConfig,
  updateWebResearchConfig,
} from '@/capabilities/capabilityClient';
import type { CapabilityControlPlane } from '@/types/capabilities';
import { useCapabilityAdminStore } from './capabilityAdmin';

vi.mock('@/capabilities/capabilityClient', () => ({
  createManagedSkill: vi.fn(),
  createManagedTool: vi.fn(),
  deleteManagedSkill: vi.fn(),
  deleteManagedTool: vi.fn(),
  getCapabilityControlPlane: vi.fn(),
  updateManagedSkill: vi.fn(),
  updateManagedTool: vi.fn(),
  updateSqlAssistantConfig: vi.fn(),
  updateWebResearchConfig: vi.fn(),
}));

function controlPlane(overrides: Partial<CapabilityControlPlane> = {}): CapabilityControlPlane {
  return {
    schema_version: 1,
    web_research: { enabled: true, provider: 'tavily-keyless', api_key_required: false },
    sql_assistant: {
      enabled: false,
      dsn_secret_name: 'SQL_ASSISTANT_DSN',
      dsn_configured: false,
      expected_role: '',
      allowed_schemas: [],
      allowed_tables: [],
      sensitive_columns: [],
      statement_timeout_seconds: 10,
      max_rows: 200,
      max_result_bytes: 262144,
      max_estimated_cost: 100000,
      max_estimated_rows: 100000,
      max_estimated_bytes: 8388608,
      catalog_cache_ttl_seconds: 300,
      updated_at: '2026-07-21T00:00:00Z',
    },
    skills: [],
    custom_tools: [],
    builtin_tools: [
      {
        name: 'web_search',
        description: 'Search public web evidence.',
        group: 'web-research',
        version: '1.0.0',
        required_roles: [],
        requires_approval: false,
        network_policy: 'restricted',
        resource_scope: 'public-web',
      },
    ],
    ...overrides,
  };
}

describe('capability admin store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('loads the admin control plane and derives selectable Tool names', async () => {
    vi.mocked(getCapabilityControlPlane).mockResolvedValue(
      controlPlane({
        custom_tools: [
          {
            name: 'release_lookup',
            version: '1.0.0',
            description: 'Lookup releases.',
            group: 'custom-http',
            endpoint: 'https://api.vendor.dev/releases',
            method: 'POST',
            input_schema: { type: 'object' },
            static_headers: {},
            secret_headers: {},
            required_roles: [],
            requires_approval: false,
            idempotent: true,
            timeout_seconds: 20,
            max_response_bytes: 65536,
            enabled: true,
            created_at: '2026-07-21T00:00:00Z',
            updated_at: '2026-07-21T00:00:00Z',
          },
        ],
      })
    );
    const store = useCapabilityAdminStore();

    await store.fetchControlPlane();

    expect(store.loading).toBe(false);
    expect(store.availableToolNames).toEqual(['release_lookup', 'web_search']);
  });

  it('updates the active configuration immediately', async () => {
    vi.mocked(updateWebResearchConfig).mockResolvedValue(
      controlPlane({
        web_research: { enabled: false, provider: 'tavily-keyless', api_key_required: false },
      })
    );
    const store = useCapabilityAdminStore();

    await store.updateWebResearch(false);

    expect(store.controlPlane?.web_research.enabled).toBe(false);
    expect(store.notice).toContain('已停用');
  });

  it('creates a custom Skill and refreshes after a delete response', async () => {
    vi.mocked(createManagedSkill).mockResolvedValue(controlPlane());
    vi.mocked(deleteManagedTool).mockResolvedValue({ name: 'release_lookup', deleted: true });
    vi.mocked(getCapabilityControlPlane).mockResolvedValue(
      controlPlane({
        web_research: { enabled: false, provider: 'tavily-keyless', api_key_required: false },
      })
    );
    const store = useCapabilityAdminStore();

    await store.createSkill('release-research', {
      description: 'Research releases',
      instructions: '# Workflow',
      allowed_tools: ['release_lookup'],
      required_roles: [],
      required_secrets: [],
      enabled: true,
    });
    await store.deleteTool('release_lookup');

    expect(createManagedSkill).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'release-research', allowed_tools: ['release_lookup'] })
    );
    expect(getCapabilityControlPlane).toHaveBeenCalledOnce();
    expect(store.controlPlane?.web_research.enabled).toBe(false);
    expect(store.notice).toContain('已删除 Tool');
  });

  it('normalizes mutation failures and keeps the last durable projection', async () => {
    vi.mocked(updateSqlAssistantConfig).mockRejectedValue(new TypeError('secret transport detail'));
    const store = useCapabilityAdminStore();
    store.controlPlane = controlPlane();

    await expect(
      store.updateSqlAssistant({
        enabled: false,
        dsn_secret_name: 'SQL_ASSISTANT_DSN',
        expected_role: '',
        allowed_schemas: [],
        allowed_tables: [],
        sensitive_columns: [],
        statement_timeout_seconds: 10,
        max_rows: 200,
        max_result_bytes: 262144,
        max_estimated_cost: 100000,
        max_estimated_rows: 100000,
        max_estimated_bytes: 8388608,
        catalog_cache_ttl_seconds: 300,
      })
    ).rejects.toMatchObject({ code: 'NETWORK_UNAVAILABLE' });

    expect(store.controlPlane?.web_research.enabled).toBe(true);
    expect(store.error).toBe('无法连接服务，请检查网络后重试');
  });
});
