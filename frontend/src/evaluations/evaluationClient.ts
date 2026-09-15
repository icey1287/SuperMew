import api from '@/utils/api';
import type {
  RagEvaluationCaseResult,
  RagEvaluationDataset,
  RagEvaluationDatasetRecord,
  RagEvaluationJob,
  RagEvaluationJobCreatePayload,
  RagEvaluationJobStatus,
} from '@/types/evaluations';

export async function listRagEvaluationDatasets(): Promise<RagEvaluationDatasetRecord[]> {
  return (await api.get<{ datasets: RagEvaluationDatasetRecord[] }>('/v1/rag-evaluations/datasets'))
    .data.datasets;
}

export async function createRagEvaluationDataset(
  dataset: RagEvaluationDataset
): Promise<RagEvaluationDatasetRecord> {
  return (await api.post<RagEvaluationDatasetRecord>('/v1/rag-evaluations/datasets', { dataset }))
    .data;
}

export async function listRagEvaluationJobs(
  status?: RagEvaluationJobStatus | null
): Promise<RagEvaluationJob[]> {
  return (
    await api.get<{ jobs: RagEvaluationJob[] }>('/v1/rag-evaluations/jobs', {
      params: status ? { status } : undefined,
    })
  ).data.jobs;
}

export async function createRagEvaluationJob(
  payload: RagEvaluationJobCreatePayload
): Promise<RagEvaluationJob> {
  return (await api.post<RagEvaluationJob>('/v1/rag-evaluations/jobs', payload)).data;
}

export async function getRagEvaluationJob(jobId: string): Promise<RagEvaluationJob> {
  return (await api.get<RagEvaluationJob>(`/v1/rag-evaluations/jobs/${jobId}`)).data;
}

export async function listRagEvaluationCases(jobId: string): Promise<RagEvaluationCaseResult[]> {
  return (
    await api.get<{ cases: RagEvaluationCaseResult[] }>(`/v1/rag-evaluations/jobs/${jobId}/cases`)
  ).data.cases;
}

export async function cancelRagEvaluationJob(jobId: string): Promise<RagEvaluationJob> {
  return (await api.post<RagEvaluationJob>(`/v1/rag-evaluations/jobs/${jobId}/cancel`)).data;
}
