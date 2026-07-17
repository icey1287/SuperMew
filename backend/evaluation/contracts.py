from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.evaluation.rag import (
    RagEvalDataset,
    RagEvalGatePolicy,
    RagEvalObservation,
    RagEvalReport,
)
from backend.model_control import ModelCatalogSnapshot


class RagEvaluationJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RagEvaluationCaseStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RagEvaluationDatasetRecord(EvaluationContract):
    id: str
    name: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(ge=1)
    dataset: RagEvalDataset
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class RagEvaluationJobRecord(EvaluationContract):
    id: str
    dataset_id: str
    dataset_name: str
    dataset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_job_id: str | None = None
    status: RagEvaluationJobStatus
    completed_cases: int = Field(ge=0)
    total_cases: int = Field(ge=1)
    gate_policy: RagEvalGatePolicy
    model_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_snapshot: ModelCatalogSnapshot
    owner_worker_id: str | None = None
    lease_expires_at: datetime | None = None
    fencing_token: int = Field(ge=0)
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    error_code: str | None = None
    error: dict | None = None
    report: RagEvalReport | None = None
    created_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "lease_expires_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class RagEvaluationCaseRecord(EvaluationContract):
    id: str
    job_id: str
    case_id: str
    position: int = Field(ge=1)
    status: RagEvaluationCaseStatus
    question: str
    generated_answer: str | None = None
    judge_reason: str | None = None
    observation: RagEvalObservation | None = None
    judge: dict | None = None
    metrics: dict[str, float | None] = Field(default_factory=dict)
    checks: dict[str, bool | None] = Field(default_factory=dict)
    retrieved_identities: tuple[dict, ...] = ()
    provider_error_code: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("started_at", "finished_at", "created_at", "updated_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ClaimedRagEvaluationJob(EvaluationContract):
    job: RagEvaluationJobRecord
    dataset: RagEvaluationDatasetRecord
    cases: tuple[RagEvaluationCaseRecord, ...]


__all__ = [
    "ClaimedRagEvaluationJob",
    "RagEvaluationCaseRecord",
    "RagEvaluationCaseStatus",
    "RagEvaluationDatasetRecord",
    "RagEvaluationJobRecord",
    "RagEvaluationJobStatus",
]
