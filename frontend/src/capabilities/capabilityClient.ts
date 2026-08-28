import api, { getPublicError } from '@/utils/api';
import type {
  CapabilityCatalogResponse,
  CapabilityControlPlane,
  CapabilityDeleteResponse,
  ManagedHttpToolPayload,
  ManagedSkillPayload,
  SqlAssistantConfigPayload,
} from '@/types/capabilities';

export async function getCapabilityCatalog(): Promise<CapabilityCatalogResponse> {
  try {
    return (await api.get<CapabilityCatalogResponse>('/v1/capabilities')).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

async function controlRequest<T>(request: Promise<{ data: T }>): Promise<T> {
  try {
    return (await request).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export function getCapabilityControlPlane(): Promise<CapabilityControlPlane> {
  return controlRequest(api.get<CapabilityControlPlane>('/v1/capabilities/control-plane'));
}

export function createManagedSkill(
  payload: ManagedSkillPayload & { name: string }
): Promise<CapabilityControlPlane> {
  return controlRequest(api.post<CapabilityControlPlane>('/v1/capabilities/skills', payload));
}

export function updateManagedSkill(
  name: string,
  payload: ManagedSkillPayload
): Promise<CapabilityControlPlane> {
  return controlRequest(
    api.put<CapabilityControlPlane>(`/v1/capabilities/skills/${encodeURIComponent(name)}`, payload)
  );
}

export function deleteManagedSkill(name: string): Promise<CapabilityDeleteResponse> {
  return controlRequest(
    api.delete<CapabilityDeleteResponse>(`/v1/capabilities/skills/${encodeURIComponent(name)}`)
  );
}

export function createManagedTool(
  payload: ManagedHttpToolPayload & { name: string }
): Promise<CapabilityControlPlane> {
  return controlRequest(api.post<CapabilityControlPlane>('/v1/capabilities/tools', payload));
}

export function updateManagedTool(
  name: string,
  payload: ManagedHttpToolPayload
): Promise<CapabilityControlPlane> {
  return controlRequest(
    api.put<CapabilityControlPlane>(`/v1/capabilities/tools/${encodeURIComponent(name)}`, payload)
  );
}

export function deleteManagedTool(name: string): Promise<CapabilityDeleteResponse> {
  return controlRequest(
    api.delete<CapabilityDeleteResponse>(`/v1/capabilities/tools/${encodeURIComponent(name)}`)
  );
}

export function updateSqlAssistantConfig(
  payload: SqlAssistantConfigPayload
): Promise<CapabilityControlPlane> {
  return controlRequest(api.put<CapabilityControlPlane>('/v1/capabilities/sql-assistant', payload));
}

export function updateWebResearchConfig(enabled: boolean): Promise<CapabilityControlPlane> {
  return controlRequest(
    api.put<CapabilityControlPlane>('/v1/capabilities/web-research', { enabled })
  );
}
