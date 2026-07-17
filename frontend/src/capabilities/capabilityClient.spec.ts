import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import { getCapabilityCatalog } from './capabilityClient';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: {
      get: vi.fn(),
    },
  };
});

describe('capability client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads the authenticated canonical capability catalog', async () => {
    const catalog = {
      schema_version: 1 as const,
      catalog_hash: 'a'.repeat(64),
      skills: [],
      tools: [],
    };
    vi.mocked(api.get).mockResolvedValue({ data: catalog });

    await expect(getCapabilityCatalog()).resolves.toEqual(catalog);

    expect(api.get).toHaveBeenCalledOnce();
    expect(api.get).toHaveBeenCalledWith('/v1/capabilities');
  });

  it('normalizes transport failures before exposing them to the store', async () => {
    vi.mocked(api.get).mockRejectedValue(new TypeError('private socket detail'));

    await expect(getCapabilityCatalog()).rejects.toMatchObject({
      code: 'NETWORK_UNAVAILABLE',
      retryable: true,
      message: '无法连接服务，请检查网络后重试',
    });
  });
});
