import api from '@/utils/api';
import type {
  CapabilityCatalogResponse,
  CapabilityControlPlane,
  CapabilityDeleteResponse,
  ManagedHttpToolPayload,
  ManagedSkillPayload,
  SqlAssistantConfigPayload,
} from '@/types/capabilities';

export async function getCapabilityCatalog(): Promise<CapabilityCatalogResponse> {
  return (await api.get<CapabilityCatalogResponse>('/v1/capabilities')).data;
}

export async function getCapabilityControlPlane(): Promise<CapabilityControlPlane> {
  return (await api.get<CapabilityControlPlane>('/v1/capabilities/control-plane')).data;
}

export async function createManagedSkill(
  payload: ManagedSkillPayload & { name: string }
): Promise<CapabilityControlPlane> {
  return (await api.post<CapabilityControlPlane>('/v1/capabilities/skills', payload)).data;
}

export async function updateManagedSkill(
  name: string,
  payload: ManagedSkillPayload
): Promise<CapabilityControlPlane> {
  return (
    await api.put<CapabilityControlPlane>(
      `/v1/capabilities/skills/${encodeURIComponent(name)}`,
      payload
    )
  ).data;
}

export async function deleteManagedSkill(name: string): Promise<CapabilityDeleteResponse> {
  return (
    await api.delete<CapabilityDeleteResponse>(
      `/v1/capabilities/skills/${encodeURIComponent(name)}`
    )
  ).data;
}

export async function createManagedTool(
  payload: ManagedHttpToolPayload & { name: string }
): Promise<CapabilityControlPlane> {
  return (await api.post<CapabilityControlPlane>('/v1/capabilities/tools', payload)).data;
}

export async function updateManagedTool(
  name: string,
  payload: ManagedHttpToolPayload
): Promise<CapabilityControlPlane> {
  return (
    await api.put<CapabilityControlPlane>(
      `/v1/capabilities/tools/${encodeURIComponent(name)}`,
      payload
    )
  ).data;
}

export async function deleteManagedTool(name: string): Promise<CapabilityDeleteResponse> {
  return (
    await api.delete<CapabilityDeleteResponse>(`/v1/capabilities/tools/${encodeURIComponent(name)}`)
  ).data;
}

export async function updateSqlAssistantConfig(
  payload: SqlAssistantConfigPayload
): Promise<CapabilityControlPlane> {
  return (await api.put<CapabilityControlPlane>('/v1/capabilities/sql-assistant', payload)).data;
}

export async function updateWebResearchConfig(enabled: boolean): Promise<CapabilityControlPlane> {
  return (await api.put<CapabilityControlPlane>('/v1/capabilities/web-research', { enabled })).data;
}
