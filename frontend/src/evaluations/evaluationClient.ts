import api, { getPublicError } from '@/utils/api';
import type {
  RagEvaluationCaseResult,
  RagEvaluationDataset,
  RagEvaluationDatasetRecord,
  RagEvaluationJob,
  RagEvaluationJobCreatePayload,
  RagEvaluationJobStatus,
} from '@/types/evaluations';

export async function listRagEvaluationDatasets(): Promise<RagEvaluationDatasetRecord[]> {
  try {
    return (
      await api.get<{ datasets: RagEvaluationDatasetRecord[] }>('/v1/rag-evaluations/datasets')
    ).data.datasets;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function createRagEvaluationDataset(
  dataset: RagEvaluationDataset
): Promise<RagEvaluationDatasetRecord> {
  try {
    return (await api.post<RagEvaluationDatasetRecord>('/v1/rag-evaluations/datasets', { dataset }))
      .data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function listRagEvaluationJobs(
  status?: RagEvaluationJobStatus | null
): Promise<RagEvaluationJob[]> {
  try {
    return (
      await api.get<{ jobs: RagEvaluationJob[] }>('/v1/rag-evaluations/jobs', {
        params: status ? { status } : undefined,
      })
    ).data.jobs;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function createRagEvaluationJob(
  payload: RagEvaluationJobCreatePayload
): Promise<RagEvaluationJob> {
  try {
    return (await api.post<RagEvaluationJob>('/v1/rag-evaluations/jobs', payload)).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function getRagEvaluationJob(jobId: string): Promise<RagEvaluationJob> {
  try {
    return (await api.get<RagEvaluationJob>(`/v1/rag-evaluations/jobs/${jobId}`)).data;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function listRagEvaluationCases(jobId: string): Promise<RagEvaluationCaseResult[]> {
  try {
    return (
      await api.get<{ cases: RagEvaluationCaseResult[] }>(`/v1/rag-evaluations/jobs/${jobId}/cases`)
    ).data.cases;
  } catch (error) {
    throw getPublicError(error);
  }
}

export async function cancelRagEvaluationJob(jobId: string): Promise<RagEvaluationJob> {
  try {
    return (await api.post<RagEvaluationJob>(`/v1/rag-evaluations/jobs/${jobId}/cancel`)).data;
  } catch (error) {
    throw getPublicError(error);
  }
}
