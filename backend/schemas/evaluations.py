from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.evaluation.contracts import (
    RagEvaluationCaseStatus,
    RagEvaluationJobStatus,
)
from backend.evaluation.rag import (
    RagEvalDataset,
    RagEvalGatePolicy,
    RagEvalObservation,
    RagEvalReport,
)


class EvaluationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RagEvaluationDatasetCreateRequest(EvaluationSchema):
    dataset: RagEvalDataset


class RagEvaluationDatasetResponse(EvaluationSchema):
    id: str
    name: str
    fingerprint: str
    case_count: int
    dataset: RagEvalDataset
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class RagEvaluationDatasetsResponse(EvaluationSchema):
    datasets: tuple[RagEvaluationDatasetResponse, ...]


class RagEvaluationJobCreateRequest(EvaluationSchema):
    dataset_id: str = Field(min_length=1, max_length=64)
    baseline_job_id: str | None = Field(default=None, max_length=64)
    gate_policy: RagEvalGatePolicy | None = None


class EvaluationModelSummary(EvaluationSchema):
    profile_id: str
    profile_version: int
    display_name: str
    provider: str
    model_name: str
    timeout_seconds: float
    supports_stream: bool
    supports_structured_output: bool


class RagEvaluationJobResponse(EvaluationSchema):
    id: str
    dataset_id: str
    dataset_name: str
    dataset_fingerprint: str
    baseline_job_id: str | None = None
    status: RagEvaluationJobStatus
    completed_cases: int
    total_cases: int
    progress: float = Field(ge=0, le=1)
    gate_policy: RagEvalGatePolicy
    model_catalog_hash: str
    models: dict[str, EvaluationModelSummary]
    owner_worker_id: str | None = None
    lease_expires_at: datetime | None = None
    fencing_token: int
    attempts: int
    max_attempts: int
    error_code: str | None = None
    error: dict | None = None
    report: RagEvalReport | None = None
    created_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RagEvaluationJobsResponse(EvaluationSchema):
    jobs: tuple[RagEvaluationJobResponse, ...]


class RagEvaluationCaseResponse(EvaluationSchema):
    id: str
    job_id: str
    case_id: str
    position: int
    status: RagEvaluationCaseStatus
    question: str
    generated_answer: str | None = None
    judge_reason: str | None = None
    observation: RagEvalObservation | None = None
    judge: dict | None = None
    metrics: dict[str, float | None]
    checks: dict[str, bool | None]
    retrieved_identities: tuple[dict, ...]
    provider_error_code: str | None = None
    provider_error_stage: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RagEvaluationCasesResponse(EvaluationSchema):
    cases: tuple[RagEvaluationCaseResponse, ...]


__all__ = [
    "EvaluationModelSummary",
    "RagEvaluationCaseResponse",
    "RagEvaluationCasesResponse",
    "RagEvaluationDatasetCreateRequest",
    "RagEvaluationDatasetResponse",
    "RagEvaluationDatasetsResponse",
    "RagEvaluationJobCreateRequest",
    "RagEvaluationJobResponse",
    "RagEvaluationJobsResponse",
]
