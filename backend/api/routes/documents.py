from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from backend.api.resources import (
    delete_document_transactionally,
    document_catalog,
    document_publication,
    ensure_upload_dir,
)
from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.documents.catalog import (
    CleanupJobStatus,
    DocumentRecord,
)
from backend.infra.auth import require_admin
from backend.schemas import (
    DocumentDeleteJobResponse,
    DocumentDeleteStartResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentUploadJobResponse,
    DocumentUploadStartResponse,
)
from backend.security.uploads import store_upload


router = APIRouter(tags=["documents"])


def _display_file_type(filename: str, media_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "PDF"
    if suffix in {".doc", ".docx"}:
        return "Word"
    if suffix in {".xls", ".xlsx"}:
        return "Excel"
    if suffix in {".html", ".htm"}:
        return "HTML"
    return media_type or "Document"


def _document_info(record: DocumentRecord) -> DocumentInfo:
    version = record.current_version or record.pending_version
    return DocumentInfo(
        document_id=record.id,
        filename=record.canonical_name,
        file_type=_display_file_type(
            record.canonical_name,
            version.media_type if version else "",
        ),
        chunk_count=(
            record.current_version.chunk_count if record.current_version else 0
        ),
        current_version_id=(
            record.current_version.id if record.current_version else None
        ),
        pending_version_id=(
            record.pending_version.id if record.pending_version else None
        ),
        version_number=(version.version_number if version else None),
        status=record.status,
        parent_chunk_count=(
            record.current_version.parent_chunk_count if record.current_version else 0
        ),
        size_bytes=(version.size_bytes if version else 0),
        uploaded_at=(
            (version.published_at or version.created_at).isoformat()
            if version
            else None
        ),
        build_fingerprint=(version.build_fingerprint if version else None),
        parser_version=(version.parser_version if version else None),
        chunker_version=(version.chunker_version if version else None),
        embedding_model=(version.embedding_model if version else None),
        index_version=(version.index_version if version else None),
        vector_collection=(version.vector_collection if version else None),
        error_code=(version.error_code if version else None),
    )


def _list_documents_sync() -> list[DocumentInfo]:
    documents: list[DocumentInfo] = []
    offset = 0
    while True:
        page = document_catalog.list_documents(
            tenant_id=document_publication.config.tenant_id,
            include_deleted=False,
            limit=1000,
            offset=offset,
        )
        documents.extend(_document_info(record) for record in page)
        if len(page) < 1000:
            return documents
        offset += len(page)


_DELETE_STEPS = (
    ("prepare", "原子撤销检索范围"),
    ("milvus", "清理向量索引"),
    ("parent_store", "清理父级分块与缓存"),
    ("object_store", "清理版本对象"),
    ("finalize", "确认清理状态"),
)


def _delete_job_view_sync(retirement_job_id: str) -> dict:
    tenant_id = document_publication.config.tenant_id
    operation = document_catalog.get_retirement_job(
        job_id=retirement_job_id,
        tenant_id=tenant_id,
    )
    jobs = document_catalog.list_cleanup_jobs_for_versions(
        document_version_ids=operation.cleanup_version_ids,
        tenant_id=tenant_id,
    )
    expected_version_ids = tuple(dict.fromkeys(operation.cleanup_version_ids))
    actual_version_ids = {item.version.id for item in jobs}
    missing_version_ids = tuple(
        version_id
        for version_id in expected_version_ids
        if version_id not in actual_version_ids
    )
    operation_failed = bool(operation.error_code)
    ledger_incomplete = bool(missing_version_ids)
    view_failure_code = operation.error_code or (
        "CLEANUP_JOB_MISSING" if ledger_incomplete else None
    )
    completed = not view_failure_code and all(
        item.job.status == CleanupJobStatus.COMPLETED for item in jobs
    )
    dead_letter_jobs = [
        item for item in jobs if item.job.status == CleanupJobStatus.DEAD_LETTER
    ]
    dead_letter = bool(dead_letter_jobs)
    if operation_failed:
        status = "failed"
        message = "文档删除 scope 未能完整持久化，请重试"
    elif ledger_incomplete:
        status = "failed"
        message = "文档清理账本不完整，不能确认物理数据已清理"
    elif completed:
        status = "completed"
        message = "文档物理数据已完成清理"
    elif dead_letter:
        status = "cleanup_failed"
        message = "文档已不可检索，但物理清理失败，需要管理员受控重试"
    else:
        status = "running"
        message = "文档已不可检索，持久化 worker 正在清理物理数据"

    diagnostic = next(
        iter(dead_letter_jobs),
        next(
            (item for item in jobs if item.job.status != CleanupJobStatus.COMPLETED),
            jobs[-1] if jobs else None,
        ),
    )
    if operation_failed:
        current_step = "prepare"
    elif ledger_incomplete:
        current_step = "finalize"
    elif completed:
        current_step = "finalize"
    elif diagnostic is None:
        current_step = "milvus"
    else:
        raw_step = (
            diagnostic.job.step_state.get("failed_step")
            if diagnostic.job.status == CleanupJobStatus.DEAD_LETTER
            else diagnostic.job.current_step
        )
        current_step = (
            raw_step if raw_step in {key for key, _label in _DELETE_STEPS} else "milvus"
        )
    current_index = next(
        index
        for index, (key, _label) in enumerate(_DELETE_STEPS)
        if key == current_step
    )
    steps = []
    for index, (key, label) in enumerate(_DELETE_STEPS):
        if completed:
            step_status, percent = "completed", 100
        elif operation_failed:
            step_status = "failed" if key == "prepare" else "pending"
            percent = 100 if key == "prepare" else 0
        elif ledger_incomplete:
            step_status = "failed" if key == "finalize" else "pending"
            percent = 100 if key in {"prepare", "finalize"} else 0
        elif index < current_index:
            step_status, percent = "completed", 100
        elif index == current_index:
            step_status = "failed" if dead_letter else "running"
            percent = 100 if dead_letter else 20
        else:
            step_status, percent = "pending", 0
        steps.append(
            {
                "key": key,
                "label": label,
                "percent": percent,
                "status": step_status,
                "message": message if index == current_index else "",
            }
        )
    updated_at = max([operation.updated_at, *(item.job.updated_at for item in jobs)])
    return {
        "job_id": operation.id,
        "cleanup_job_id": diagnostic.job.id if diagnostic else None,
        "dead_letter_job_ids": [item.job.id for item in dead_letter_jobs],
        "document_id": operation.document_id,
        "document_version_id": diagnostic.version.id if diagnostic else None,
        "filename": operation.canonical_name,
        "status": status,
        "current_step": current_step,
        "message": message,
        "total_chunks": len(operation.cleanup_version_ids),
        "processed_chunks": sum(
            item.job.status == CleanupJobStatus.COMPLETED for item in jobs
        ),
        "error": view_failure_code
        or (diagnostic.job.error_code if diagnostic else None),
        "attempts": diagnostic.job.attempts if diagnostic else 0,
        "max_attempts": diagnostic.job.max_attempts if diagnostic else 0,
        "execution_fence": diagnostic.job.execution_fence if diagnostic else 0,
        "next_retry_at": (
            diagnostic.job.next_retry_at.isoformat()
            if diagnostic and diagnostic.job.next_retry_at
            else None
        ),
        "created_at": operation.created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "steps": steps,
    }


def _list_delete_job_views_sync() -> list[dict]:
    operations = document_catalog.list_retirement_jobs(
        tenant_id=document_publication.config.tenant_id,
        limit=100,
    )
    return [_delete_job_view_sync(operation.id) for operation in operations]


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(_: User = Depends(require_admin)):
    try:
        return DocumentListResponse(
            documents=await asyncio.to_thread(_list_documents_sync)
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "获取文档目录失败",
            status_code=503,
            retryable=True,
        ) from exc


@router.post(
    "/documents/upload/async",
    response_model=DocumentUploadStartResponse,
    status_code=202,
)
async def upload_document_async(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
):
    await asyncio.to_thread(ensure_upload_dir)
    try:
        stored = await store_upload(file)
        reservation = await asyncio.to_thread(
            document_publication.submit,
            stored,
            user.id,
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "文件保存或候选版本预留失败",
            status_code=503,
            retryable=True,
        ) from exc

    message = (
        "相同内容与构建版本已发布，无需重复入库"
        if reservation.already_current
        else "文件已上传，候选版本已进入持久化 worker 队列；发布前旧版本保持可用"
    )
    return DocumentUploadStartResponse(
        job_id=reservation.job.id,
        filename=reservation.document.canonical_name,
        document_id=reservation.document.id,
        document_version_id=reservation.version.id,
        version_number=reservation.version.version_number,
        status=reservation.job.status,
        message=message,
    )


@router.get(
    "/documents/upload/jobs/{job_id}",
    response_model=DocumentUploadJobResponse,
)
async def get_upload_job(job_id: str, _: User = Depends(require_admin)):
    return DocumentUploadJobResponse(
        **await asyncio.to_thread(document_publication.get_job_view, job_id)
    )


@router.get(
    "/documents/upload/jobs",
    response_model=list[DocumentUploadJobResponse],
)
async def list_upload_jobs(_: User = Depends(require_admin)):
    jobs = await asyncio.to_thread(document_publication.list_job_views)
    return [DocumentUploadJobResponse(**job) for job in jobs]


@router.delete(
    "/documents/delete/async/{filename}",
    response_model=DocumentDeleteStartResponse,
    status_code=202,
)
async def delete_document_async(
    filename: str,
    _: User = Depends(require_admin),
):
    outcome = await asyncio.to_thread(
        delete_document_transactionally,
        filename,
    )
    if not outcome.retirement_job_id:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "删除任务身份未能持久化",
            status_code=503,
            retryable=True,
        )
    return DocumentDeleteStartResponse(
        job_id=outcome.retirement_job_id,
        filename=filename,
        message=(
            f"{filename} 已从检索目录撤销，物理清理已进入持久化队列"
            if outcome.cleanup_required
            else f"{filename} 已从检索目录撤销，无待清理数据"
        ),
    )


@router.get(
    "/documents/delete/jobs/{job_id}",
    response_model=DocumentDeleteJobResponse,
)
async def get_delete_job(job_id: str, _: User = Depends(require_admin)):
    return DocumentDeleteJobResponse(
        **await asyncio.to_thread(_delete_job_view_sync, job_id)
    )


@router.get(
    "/documents/delete/jobs",
    response_model=list[DocumentDeleteJobResponse],
)
async def list_delete_jobs(_: User = Depends(require_admin)):
    jobs = await asyncio.to_thread(_list_delete_job_views_sync)
    return [DocumentDeleteJobResponse(**job) for job in jobs]
