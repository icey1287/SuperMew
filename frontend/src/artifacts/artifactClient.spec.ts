import { describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import { fetchArtifact, isFetchableArtifactUri } from './artifactClient';

vi.mock('@/utils/api', async () => {
  const actual = await vi.importActual<typeof import('@/utils/api')>('@/utils/api');
  return {
    ...actual,
    default: { get: vi.fn() },
  };
});

describe('artifact client', () => {
  it('accepts only the authenticated same-origin Artifact Interface', () => {
    expect(isFetchableArtifactUri('/api/artifacts/art_report_1')).toBe(true);
    expect(isFetchableArtifactUri('artifact://art_report_1')).toBe(false);
    expect(isFetchableArtifactUri('https://example.com/report')).toBe(false);
    expect(isFetchableArtifactUri('/api/artifacts/../secret')).toBe(false);
  });

  it('fetches an Artifact as a bearer-authenticated blob through the shared client', async () => {
    const blob = new Blob(['result'], { type: 'text/plain' });
    vi.mocked(api.get).mockResolvedValue({ data: blob } as never);

    await expect(fetchArtifact('/api/artifacts/art_result_1')).resolves.toBe(blob);
    expect(api.get).toHaveBeenCalledWith('/api/artifacts/art_result_1', {
      responseType: 'blob',
    });
  });
});
