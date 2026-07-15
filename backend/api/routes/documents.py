from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from backend.api.resources import (
    delete_document_transactionally,
    document_catalog,
    document_publication,
    ensure_upload_dir,
)
from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.documents.catalog import DocumentRecord, IndexJobStatus
from backend.infra.auth import require_admin
from backend.jobs import DELETE_STEPS, delete_job_manager
from backend.schemas import (
    DocumentDeleteJobResponse,
    DocumentDeleteResponse,
    DocumentDeleteStartResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentUploadJobResponse,
    DocumentUploadResponse,
    DocumentUploadStartResponse,
)
from backend.security.uploads import StoredUpload, store_upload


logger = logging.getLogger(__name__)
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
        storage_layout=(version.storage_layout if version else None),
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


def _store_document_sync(stored: StoredUpload, owner_id: int):
    reservation = document_publication.submit(stored, owner_id)
    return document_publication.run(reservation)


def _process_upload_job(job_id: str) -> None:
    for attempt in range(2):
        try:
            document_publication.run(job_id)
            return
        except Exception as exc:
            public_error = getattr(exc, "public_error", None)
            if attempt == 0:
                try:
                    job = document_catalog.get_job(
                        job_id=job_id,
                        tenant_id=document_publication.config.tenant_id,
                    )
                except Exception:
                    job = None
                if job is not None and job.status == IndexJobStatus.STAGED:
                    continue
            logger.warning(
                "background document publication failed",
                extra={
                    "job_id": job_id,
                    "error_code": getattr(
                        public_error,
                        "code",
                        "INDEX_BUILD_FAILED",
                    ),
                },
            )
            return


def _process_delete_job(job_id: str, filename: str, owner_id: int) -> None:
    try:
        outcome = delete_document_transactionally(
            filename,
            delete_job_manager,
            job_id,
            owner_id,
        )
        if outcome.cleanup_pending:
            delete_job_manager.mark_cleanup_pending(
                job_id,
                outcome.cleanup_step or "finalize",
                "文档已不可检索，物理数据清理待重试",
            )
            return
        delete_job_manager.complete_job(
            job_id,
            f"已从目录撤销 {filename}，清理向量数据 {outcome.chunks_deleted} 条",
        )
    except AppError as exc:
        job = delete_job_manager.get_job(job_id)
        current_step = job.get("current_step", "prepare") if job else "prepare"
        delete_job_manager.fail_job(
            job_id,
            current_step,
            (
                "legacy 删除封印未完成，请重试删除"
                if exc.stage == "catalog_tombstone"
                else "文档目录撤销失败，请稍后重试"
            ),
        )
    except Exception:
        job = delete_job_manager.get_job(job_id)
        current_step = job.get("current_step", "prepare") if job else "prepare"
        delete_job_manager.fail_job(
            job_id,
            current_step,
            "文档已撤销或清理失败，请稍后重试",
        )


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


@router.post("/documents/upload/async", response_model=DocumentUploadStartResponse)
async def upload_document_async(
    background_tasks: BackgroundTasks,
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

    if reservation.job.status in {
        IndexJobStatus.PENDING,
        IndexJobStatus.RETRY_WAIT,
        IndexJobStatus.STAGED,
    }:
        background_tasks.add_task(_process_upload_job, reservation.job.id)
    message = (
        "相同内容与构建版本已发布，无需重复入库"
        if reservation.already_current
        else "文件已上传，候选版本正在后台构建；发布前旧版本保持可用"
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
)
async def delete_document_async(
    filename: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
):
    job = delete_job_manager.create_job(
        filename,
        steps=DELETE_STEPS,
        current_step="prepare",
        message="等待从目录撤销",
        completion_step="finalize",
    )
    delete_job_manager.update_step(
        job["job_id"],
        "prepare",
        1,
        "running",
        "删除任务已提交",
    )
    background_tasks.add_task(
        _process_delete_job,
        job["job_id"],
        filename,
        user.id,
    )
    return DocumentDeleteStartResponse(
        job_id=job["job_id"],
        filename=filename,
        message=f"正在从检索目录撤销 {filename}",
    )


@router.get(
    "/documents/delete/jobs/{job_id}",
    response_model=DocumentDeleteJobResponse,
)
async def get_delete_job(job_id: str, _: User = Depends(require_admin)):
    job = delete_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="删除任务不存在或已过期")
    return DocumentDeleteJobResponse(**job)


@router.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
):
    try:
        await asyncio.to_thread(ensure_upload_dir)
        stored = await store_upload(file)
        outcome = await asyncio.to_thread(_store_document_sync, stored, user.id)
        return DocumentUploadResponse(
            filename=outcome.document.canonical_name,
            chunks_processed=outcome.vector_chunk_count,
            document_id=outcome.document.id,
            document_version_id=outcome.version.id,
            version_number=outcome.version.version_number,
            published=outcome.published,
            reused_current=outcome.reused_current,
            message=(
                "相同内容与构建版本已存在，沿用当前版本"
                if outcome.reused_current
                else (
                    f"版本 v{outcome.version.version_number} 已原子发布："
                    f"叶子分块 {outcome.vector_chunk_count} 个，"
                    f"父级分块 {outcome.parent_chunk_count} 个"
                )
            ),
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "文档上传失败，旧版本仍保持可用",
            status_code=503,
            retryable=True,
        ) from exc


@router.delete("/documents/{filename}", response_model=DocumentDeleteResponse)
async def delete_document(filename: str, user: User = Depends(require_admin)):
    try:
        outcome = await asyncio.to_thread(
            delete_document_transactionally,
            filename,
            owner_id=user.id,
        )
        return DocumentDeleteResponse(
            filename=filename,
            document_id=outcome.document_id,
            chunks_deleted=outcome.chunks_deleted,
            status=("cleanup_pending" if outcome.cleanup_pending else "completed"),
            cleanup_pending=outcome.cleanup_pending,
            error_code=outcome.cleanup_error_code,
            message=(
                "文档已不可检索，物理数据清理待重试"
                if outcome.cleanup_pending
                else (
                    f"文档 {filename} 已从目录撤销，并清理 "
                    f"{outcome.chunks_deleted} 条向量数据"
                )
            ),
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "删除文档失败",
            status_code=503,
            retryable=True,
        ) from exc
