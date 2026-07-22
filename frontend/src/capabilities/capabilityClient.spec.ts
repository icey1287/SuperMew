import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import {
  createManagedSkill,
  createManagedTool,
  deleteManagedSkill,
  deleteManagedTool,
  getCapabilityControlPlane,
  updateManagedSkill,
  updateManagedTool,
  updateSqlAssistantConfig,
  updateWebResearchConfig,
} from './capabilityClient';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: {
      delete: vi.fn(),
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
    },
  };
});

describe('capability admin client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses the canonical control-plane and Skill endpoints', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { schema_version: 1 } } as any);
    vi.mocked(api.post).mockResolvedValue({ data: { schema_version: 1 } } as any);
    vi.mocked(api.put).mockResolvedValue({ data: { schema_version: 1 } } as any);
    vi.mocked(api.delete).mockResolvedValue({ data: { name: 'release-research', deleted: true } });

    await getCapabilityControlPlane();
    await createManagedSkill({
      name: 'release-research',
      description: 'Research releases',
      instructions: '# Workflow',
      allowed_tools: [],
      required_roles: [],
      required_secrets: [],
      enabled: true,
    });
    await updateManagedSkill('release/research', {
      description: 'Research releases',
      instructions: '# Workflow',
      allowed_tools: [],
      required_roles: [],
      required_secrets: [],
      enabled: false,
    });
    await deleteManagedSkill('release-research');

    expect(api.get).toHaveBeenCalledWith('/v1/capabilities/control-plane');
    expect(api.post).toHaveBeenCalledWith(
      '/v1/capabilities/skills',
      expect.objectContaining({ name: 'release-research' })
    );
    expect(api.put).toHaveBeenCalledWith(
      '/v1/capabilities/skills/release%2Fresearch',
      expect.objectContaining({ enabled: false })
    );
    expect(api.delete).toHaveBeenCalledWith('/v1/capabilities/skills/release-research');
  });

  it('uses custom Tool and provider configuration endpoints', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { schema_version: 1 } } as any);
    vi.mocked(api.put).mockResolvedValue({ data: { schema_version: 1 } } as any);
    vi.mocked(api.delete).mockResolvedValue({ data: { name: 'release_lookup', deleted: true } });
    const toolPayload = {
      description: 'Lookup releases',
      group: 'custom-http',
      endpoint: 'https://api.vendor.dev/releases',
      method: 'POST' as const,
      input_schema: { type: 'object' },
      static_headers: {},
      secret_headers: {},
      required_roles: [],
      requires_approval: false,
      idempotent: true,
      timeout_seconds: 20,
      max_response_bytes: 65536,
      enabled: true,
    };

    await createManagedTool({ name: 'release_lookup', ...toolPayload });
    await updateManagedTool('release_lookup', toolPayload);
    await deleteManagedTool('release_lookup');
    await updateWebResearchConfig(true);
    await updateSqlAssistantConfig({
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
    });

    expect(api.post).toHaveBeenCalledWith(
      '/v1/capabilities/tools',
      expect.objectContaining({ name: 'release_lookup' })
    );
    expect(api.put).toHaveBeenCalledWith('/v1/capabilities/tools/release_lookup', toolPayload);
    expect(api.delete).toHaveBeenCalledWith('/v1/capabilities/tools/release_lookup');
    expect(api.put).toHaveBeenCalledWith('/v1/capabilities/web-research', { enabled: true });
    expect(api.put).toHaveBeenCalledWith(
      '/v1/capabilities/sql-assistant',
      expect.objectContaining({ dsn_secret_name: 'SQL_ASSISTANT_DSN' })
    );
  });
});
