import api, { getPublicError } from '@/utils/api';
import type { CapabilityCatalogResponse } from '@/types/capabilities';

export async function getCapabilityCatalog(): Promise<CapabilityCatalogResponse> {
  try {
    return (await api.get<CapabilityCatalogResponse>('/v1/capabilities')).data;
  } catch (error) {
    throw getPublicError(error);
  }
}
