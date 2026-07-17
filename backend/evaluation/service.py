from __future__ import annotations

from backend.core.settings import AppSettings, get_settings
from backend.evaluation.contracts import (
    RagEvaluationCaseRecord,
    RagEvaluationDatasetRecord,
    RagEvaluationJobRecord,
    RagEvaluationJobStatus,
)
from backend.evaluation.rag import RagEvalDataset, RagEvalGatePolicy
from backend.evaluation.repository import (
    RagEvaluationRepository,
    rag_evaluation_repository,
)
from backend.model_control import ModelControlService, ModelRole, model_control_service


DEFAULT_RAG_EVALUATION_GATES = RagEvalGatePolicy(
    k_values=(5, 10),
    critical_no_regression=True,
    required_provenance="live_rag",
)


class RagEvaluationService:
    """Application Interface for persistent RAG evaluation control-plane actions."""

    def __init__(
        self,
        repository: RagEvaluationRepository = rag_evaluation_repository,
        *,
        model_control: ModelControlService = model_control_service,
        settings: AppSettings | None = None,
    ) -> None:
        self.repository = repository
        self.model_control = model_control
        self.settings = settings or get_settings()

    def create_dataset(
        self,
        *,
        username: str,
        dataset: RagEvalDataset,
    ) -> RagEvaluationDatasetRecord:
        return self.repository.create_dataset(username=username, dataset=dataset)

    def list_datasets(
        self, *, limit: int = 100
    ) -> tuple[RagEvaluationDatasetRecord, ...]:
        return self.repository.list_datasets(limit=limit)

    def get_dataset(self, dataset_id: str) -> RagEvaluationDatasetRecord:
        return self.repository.get_dataset(dataset_id)

    def create_job(
        self,
        *,
        username: str,
        dataset_id: str,
        gate_policy: RagEvalGatePolicy | None = None,
        baseline_job_id: str | None = None,
    ) -> RagEvaluationJobRecord:
        model_snapshot = self.model_control.runtime_snapshot(
            required_roles=frozenset(ModelRole)
        )
        return self.repository.create_job(
            username=username,
            dataset_id=dataset_id,
            gate_policy=gate_policy or DEFAULT_RAG_EVALUATION_GATES,
            model_snapshot=model_snapshot,
            baseline_job_id=baseline_job_id,
            max_attempts=self.settings.worker.evaluation_max_attempts,
        )

    def list_jobs(
        self,
        *,
        status: RagEvaluationJobStatus | str | None = None,
        limit: int = 100,
    ) -> tuple[RagEvaluationJobRecord, ...]:
        return self.repository.list_jobs(status=status, limit=limit)

    def get_job(self, job_id: str) -> RagEvaluationJobRecord:
        return self.repository.get_job(job_id)

    def list_cases(self, job_id: str) -> tuple[RagEvaluationCaseRecord, ...]:
        return self.repository.list_cases(job_id)

    def cancel_job(self, job_id: str) -> RagEvaluationJobRecord:
        return self.repository.cancel(job_id=job_id)


rag_evaluation_service = RagEvaluationService()


__all__ = [
    "DEFAULT_RAG_EVALUATION_GATES",
    "RagEvaluationService",
    "rag_evaluation_service",
]
