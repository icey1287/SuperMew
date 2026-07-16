from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import backend.api.routes.documents as documents
from backend.core.errors import AppError, ErrorCode
from backend.documents.catalog import CleanupJobStatus


_NOW = datetime(2026, 7, 16, 12, 0, 0)


def _operation(
    job_id: str,
    *version_ids: str,
    tenant_id: str | None = None,
    error_code: str | None = None,
):
    return SimpleNamespace(
        id=job_id,
        document_id=f"document-{job_id}",
        tenant_id=tenant_id or documents.document_publication.config.tenant_id,
        canonical_name="guide.pdf",
        publication_fence=3,
        cleanup_version_ids=tuple(version_ids),
        error_code=error_code,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _cleanup(
    job_id: str,
    version_id: str,
    *,
    status: CleanupJobStatus,
    current_step: str = "milvus",
    failed_step: str | None = None,
    error_code: str | None = None,
    attempts: int = 1,
):
    return SimpleNamespace(
        job=SimpleNamespace(
            id=job_id,
            status=status,
            current_step=current_step,
            step_state=(
                {"failed_step": failed_step} if failed_step is not None else {}
            ),
            error_code=error_code,
            attempts=attempts,
            max_attempts=3,
            execution_fence=attempts,
            next_retry_at=(
                _NOW + timedelta(minutes=5)
                if status == CleanupJobStatus.RETRY_WAIT
                else None
            ),
            updated_at=_NOW + timedelta(seconds=attempts),
        ),
        version=SimpleNamespace(id=version_id),
    )


class _RetirementCatalog:
    def __init__(self, operations, jobs_by_versions=None):
        self.operations = {item.id: item for item in operations}
        self.jobs_by_versions = jobs_by_versions or {}
        self.cleanup_queries: list[tuple[tuple[str, ...], str]] = []

    def get_retirement_job(self, *, job_id: str, tenant_id: str):
        operation = self.operations.get(job_id)
        if operation is None or operation.tenant_id != tenant_id:
            raise AppError(ErrorCode.NOT_FOUND, "文档删除任务不存在", status_code=404)
        return operation

    def list_cleanup_jobs_for_versions(
        self,
        *,
        document_version_ids,
        tenant_id: str,
    ):
        version_ids = tuple(document_version_ids)
        self.cleanup_queries.append((version_ids, tenant_id))
        return list(self.jobs_by_versions.get(version_ids, ()))

    def list_retirement_jobs(self, *, tenant_id: str, limit: int):
        return [
            operation
            for operation in self.operations.values()
            if operation.tenant_id == tenant_id
        ][:limit]


@pytest.mark.asyncio
async def test_delete_job_view_fails_closed_when_cleanup_ledger_is_missing():
    operation = _operation("retire-missing", "version-present", "version-missing")
    catalog = _RetirementCatalog(
        [operation],
        {
            operation.cleanup_version_ids: [
                _cleanup(
                    "cleanup-present",
                    "version-present",
                    status=CleanupJobStatus.COMPLETED,
                    current_step="completed",
                )
            ]
        },
    )

    with patch.object(documents, "document_catalog", catalog):
        response = await documents.get_delete_job(operation.id, None)

    assert response.status == "failed"
    assert response.error == "CLEANUP_JOB_MISSING"
    assert response.current_step == "finalize"
    assert response.total_chunks == 2
    assert response.processed_chunks == 1
    finalize = next(step for step in response.steps if step.key == "finalize")
    assert finalize.status == "failed"
    assert catalog.cleanup_queries == [
        (
            operation.cleanup_version_ids,
            documents.document_publication.config.tenant_id,
        )
    ]


@pytest.mark.asyncio
async def test_delete_job_view_prioritizes_dead_letter_over_running_cleanup():
    operation = _operation("retire-mixed", "version-running", "version-dead")
    running = _cleanup(
        "cleanup-running",
        "version-running",
        status=CleanupJobStatus.RUNNING,
        current_step="parent_store",
        attempts=1,
    )
    dead_letter = _cleanup(
        "cleanup-dead",
        "version-dead",
        status=CleanupJobStatus.DEAD_LETTER,
        current_step="dead_letter",
        failed_step="object_store",
        error_code="STORAGE_UNAVAILABLE",
        attempts=3,
    )
    catalog = _RetirementCatalog(
        [operation],
        {operation.cleanup_version_ids: [running, dead_letter]},
    )

    with patch.object(documents, "document_catalog", catalog):
        response = await documents.get_delete_job(operation.id, None)

    assert response.status == "cleanup_pending"
    assert response.cleanup_job_id == "cleanup-dead"
    assert response.dead_letter_job_ids == ["cleanup-dead"]
    assert response.document_version_id == "version-dead"
    assert response.current_step == "object_store"
    assert response.error == "STORAGE_UNAVAILABLE"
    assert response.attempts == 3
    object_store = next(step for step in response.steps if step.key == "object_store")
    assert object_store.status == "failed"


@pytest.mark.parametrize(
    "failed_step",
    ["milvus", "parent_store", "object_store", "finalize"],
)
@pytest.mark.asyncio
async def test_delete_job_view_maps_the_persisted_cleanup_failure_step(failed_step):
    operation = _operation(f"retire-{failed_step}", f"version-{failed_step}")
    dead_letter = _cleanup(
        f"cleanup-{failed_step}",
        f"version-{failed_step}",
        status=CleanupJobStatus.DEAD_LETTER,
        current_step="dead_letter",
        failed_step=failed_step,
        error_code="STORAGE_UNAVAILABLE",
        attempts=3,
    )
    catalog = _RetirementCatalog(
        [operation],
        {operation.cleanup_version_ids: [dead_letter]},
    )

    with patch.object(documents, "document_catalog", catalog):
        response = await documents.get_delete_job(operation.id, None)

    assert response.current_step == failed_step
    steps = {step.key: step for step in response.steps}
    step_order = ["prepare", "milvus", "parent_store", "object_store", "finalize"]
    failed_index = step_order.index(failed_step)
    for index, key in enumerate(step_order):
        if index < failed_index:
            assert steps[key].status == "completed"
            assert steps[key].percent == 100
        elif index == failed_index:
            assert steps[key].status == "failed"
            assert steps[key].percent == 100
        else:
            assert steps[key].status == "pending"
            assert steps[key].percent == 0


@pytest.mark.asyncio
async def test_delete_job_route_hides_a_foreign_tenant_retirement_operation():
    foreign = _operation(
        "retire-foreign",
        "version-foreign",
        tenant_id="tenant-foreign",
    )
    catalog = _RetirementCatalog([foreign])

    with (
        patch.object(documents, "document_catalog", catalog),
        pytest.raises(AppError) as hidden,
    ):
        await documents.get_delete_job(foreign.id, None)

    assert hidden.value.code == ErrorCode.NOT_FOUND
    assert catalog.cleanup_queries == []


@pytest.mark.asyncio
async def test_repeated_completed_tombstones_keep_distinct_terminal_job_ids():
    first = _operation("retire-completed-1")
    second = _operation("retire-completed-2")
    catalog = _RetirementCatalog([first, second])

    with patch.object(documents, "document_catalog", catalog):
        responses = await documents.list_delete_jobs(None)

    assert [response.job_id for response in responses] == [first.id, second.id]
    assert all(response.status == "completed" for response in responses)
    assert all(response.cleanup_job_id is None for response in responses)
    assert all(response.dead_letter_job_ids == [] for response in responses)
    assert all(response.processed_chunks == 0 for response in responses)
    assert all(
        all(
            step.status == "completed" and step.percent == 100
            for step in response.steps
        )
        for response in responses
    )
