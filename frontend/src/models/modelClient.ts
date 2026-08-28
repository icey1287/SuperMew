import api, { getPublicError } from '@/utils/api';
import type {
  ModelAssignmentPayload,
  ModelControlPlane,
  ModelProfilePayload,
  ModelRole,
} from '@/types/models';

export async function getModelControlPlane(): Promise<ModelControlPlane> {
  try {
    return (await api.get<ModelControlPlane>('/v1/models')).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function createModelProfile(payload: ModelProfilePayload): Promise<ModelControlPlane> {
  try {
    return (await api.post<ModelControlPlane>('/v1/models', payload)).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function updateModelProfile(
  profileId: string,
  payload: ModelProfilePayload
): Promise<ModelControlPlane> {
  try {
    return (await api.put<ModelControlPlane>(`/v1/models/${profileId}`, payload)).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function deleteModelProfile(profileId: string): Promise<void> {
  try {
    await api.delete(`/v1/models/${profileId}`);
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function assignModelRole(
  role: ModelRole,
  payload: ModelAssignmentPayload
): Promise<ModelControlPlane> {
  try {
    return (await api.put<ModelControlPlane>(`/v1/models/assignments/${role}`, payload)).data;
  } catch (error) {
    throw getPublicError(error);
  }
}
