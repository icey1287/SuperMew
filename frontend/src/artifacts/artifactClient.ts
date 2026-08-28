import api, { getPublicError } from '@/utils/api';

const ARTIFACT_PATH = /^\/api\/artifacts\/art_[A-Za-z0-9_-]+$/;

export function isFetchableArtifactUri(uri: string | null | undefined): uri is string {
  return typeof uri === 'string' && ARTIFACT_PATH.test(uri);
}

export async function fetchArtifact(uri: string): Promise<Blob> {
  if (!isFetchableArtifactUri(uri)) {
    throw getPublicError({
      code: 'INVALID_REQUEST',
      retryable: false,
      category: 'artifact',
    });
  }
  try {
    const response = await api.get<Blob>(uri, { responseType: 'blob' });
    return response.data;
  } catch (error) {
    throw getPublicError(error);
  }
}
