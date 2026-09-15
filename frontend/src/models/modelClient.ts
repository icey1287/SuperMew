import api from '@/utils/api';
import type {
  ModelAssignmentPayload,
  ModelControlPlane,
  ModelProfilePayload,
  ModelRole,
} from '@/types/models';

export async function getModelControlPlane(): Promise<ModelControlPlane> {
  return (await api.get<ModelControlPlane>('/v1/models')).data;
}

export async function createModelProfile(payload: ModelProfilePayload): Promise<ModelControlPlane> {
  return (await api.post<ModelControlPlane>('/v1/models', payload)).data;
}

export async function updateModelProfile(
  profileId: string,
  payload: ModelProfilePayload
): Promise<ModelControlPlane> {
  return (await api.put<ModelControlPlane>(`/v1/models/${profileId}`, payload)).data;
}

export async function deleteModelProfile(profileId: string): Promise<void> {
  await api.delete(`/v1/models/${profileId}`);
}

export async function assignModelRole(
  role: ModelRole,
  payload: ModelAssignmentPayload
): Promise<ModelControlPlane> {
  return (await api.put<ModelControlPlane>(`/v1/models/assignments/${role}`, payload)).data;
}
