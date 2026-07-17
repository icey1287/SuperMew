from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from starlette.concurrency import run_in_threadpool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.evaluation.contracts import (
    RagEvaluationCaseRecord,
    RagEvaluationDatasetRecord,
    RagEvaluationJobRecord,
    RagEvaluationJobStatus,
)
from backend.evaluation.service import (
    RagEvaluationService,
    rag_evaluation_service,
)
from backend.infra.auth import get_current_user
from backend.schemas.evaluations import (
    EvaluationModelSummary,
    RagEvaluationCaseResponse,
    RagEvaluationCasesResponse,
    RagEvaluationDatasetCreateRequest,
    RagEvaluationDatasetResponse,
    RagEvaluationDatasetsResponse,
    RagEvaluationJobCreateRequest,
    RagEvaluationJobResponse,
    RagEvaluationJobsResponse,
)


router = APIRouter(prefix="/v1/rag-evaluations", tags=["rag-evaluations"])


def get_rag_evaluation_service() -> RagEvaluationService:
    return rag_evaluation_service


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            "只有管理员可以使用 RAG 评估工作台。",
            status_code=403,
            category="evaluation",
            stage="authorization",
        )


def _dataset_response(
    record: RagEvaluationDatasetRecord,
) -> RagEvaluationDatasetResponse:
    return RagEvaluationDatasetResponse(**record.model_dump())


def _job_response(record: RagEvaluationJobRecord) -> RagEvaluationJobResponse:
    models = {
        role.value: EvaluationModelSummary(
            profile_id=spec.profile_id,
            profile_version=spec.profile_version,
            display_name=spec.display_name,
            provider=spec.provider,
            model_name=spec.model_name,
            timeout_seconds=spec.timeout_seconds,
            supports_stream=spec.supports_stream,
            supports_structured_output=spec.supports_structured_output,
        )
        for role, spec in record.model_snapshot.assignments.items()
    }
    return RagEvaluationJobResponse(
        id=record.id,
        dataset_id=record.dataset_id,
        dataset_name=record.dataset_name,
        dataset_fingerprint=record.dataset_fingerprint,
        baseline_job_id=record.baseline_job_id,
        status=record.status,
        completed_cases=record.completed_cases,
        total_cases=record.total_cases,
        progress=record.completed_cases / record.total_cases,
        gate_policy=record.gate_policy,
        model_catalog_hash=record.model_catalog_hash,
        models=models,
        owner_worker_id=record.owner_worker_id,
        lease_expires_at=record.lease_expires_at,
        fencing_token=record.fencing_token,
        attempts=record.attempts,
        max_attempts=record.max_attempts,
        error_code=record.error_code,
        error=record.error,
        report=record.report,
        created_by=record.created_by,
        started_at=record.started_at,
        finished_at=record.finished_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _case_response(record: RagEvaluationCaseRecord) -> RagEvaluationCaseResponse:
    return RagEvaluationCaseResponse(**record.model_dump())


@router.get("/datasets", response_model=RagEvaluationDatasetsResponse)
async def list_datasets(
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationDatasetsResponse:
    _require_admin(current_user)
    records = await run_in_threadpool(service.list_datasets, limit=limit)
    return RagEvaluationDatasetsResponse(
        datasets=tuple(_dataset_response(record) for record in records)
    )


@router.post(
    "/datasets",
    response_model=RagEvaluationDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dataset(
    request: RagEvaluationDatasetCreateRequest,
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationDatasetResponse:
    _require_admin(current_user)
    record = await run_in_threadpool(
        service.create_dataset,
        username=current_user.username,
        dataset=request.dataset,
    )
    return _dataset_response(record)


@router.get(
    "/datasets/{dataset_id}",
    response_model=RagEvaluationDatasetResponse,
)
async def get_dataset(
    dataset_id: str,
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationDatasetResponse:
    _require_admin(current_user)
    return _dataset_response(await run_in_threadpool(service.get_dataset, dataset_id))


@router.get("/jobs", response_model=RagEvaluationJobsResponse)
async def list_jobs(
    job_status: RagEvaluationJobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationJobsResponse:
    _require_admin(current_user)
    records = await run_in_threadpool(
        service.list_jobs,
        status=job_status,
        limit=limit,
    )
    return RagEvaluationJobsResponse(
        jobs=tuple(_job_response(record) for record in records)
    )


@router.post(
    "/jobs",
    response_model=RagEvaluationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_job(
    request: RagEvaluationJobCreateRequest,
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationJobResponse:
    _require_admin(current_user)
    record = await run_in_threadpool(
        service.create_job,
        username=current_user.username,
        dataset_id=request.dataset_id,
        gate_policy=request.gate_policy,
        baseline_job_id=request.baseline_job_id,
    )
    return _job_response(record)


@router.get("/jobs/{job_id}", response_model=RagEvaluationJobResponse)
async def get_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationJobResponse:
    _require_admin(current_user)
    return _job_response(await run_in_threadpool(service.get_job, job_id))


@router.get(
    "/jobs/{job_id}/cases",
    response_model=RagEvaluationCasesResponse,
)
async def list_cases(
    job_id: str,
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationCasesResponse:
    _require_admin(current_user)
    records = await run_in_threadpool(service.list_cases, job_id)
    return RagEvaluationCasesResponse(
        cases=tuple(_case_response(record) for record in records)
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=RagEvaluationJobResponse,
)
async def cancel_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    service: RagEvaluationService = Depends(get_rag_evaluation_service),
) -> RagEvaluationJobResponse:
    _require_admin(current_user)
    return _job_response(await run_in_threadpool(service.cancel_job, job_id))


__all__ = ["get_rag_evaluation_service", "router"]
