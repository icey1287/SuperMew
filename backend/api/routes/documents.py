from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from backend.api.resources import (
    delete_document_transactionally,
    ensure_upload_dir,
    loader,
    milvus_manager,
    milvus_writer,
    parent_chunk_store,
)
from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.infra.auth import require_admin
from backend.jobs import DELETE_STEPS, delete_job_manager, upload_job_manager
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
from backend.security.uploads import store_upload

router = APIRouter(tags=["documents"])


def _process_upload_job(job_id: str, file_path: str, filename: str) -> None:
    failed_step = "cleanup"
    try:
        upload_job_manager.complete_step(job_id, "upload", "文件已保存到服务器")

        failed_step = "cleanup"
        upload_job_manager.update_step(
            job_id, "cleanup", 10, "running", "正在清理同名旧文档"
        )
        delete_document_transactionally(filename)
        upload_job_manager.complete_step(job_id, "cleanup", "旧版本清理完成")

        failed_step = "parse"
        upload_job_manager.update_step(
            job_id, "parse", 5, "running", "正在解析文档并执行三级分块"
        )
        new_docs = loader.load_document(file_path, filename)
        if not new_docs:
            raise ValueError("文档处理失败，未能提取内容")

        parent_docs = [
            doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) in (1, 2)
        ]
        leaf_docs = [
            doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) == 3
        ]
        if not leaf_docs:
            raise ValueError("文档处理失败，未生成可检索叶子分块")
        upload_job_manager.complete_step(
            job_id,
            "parse",
            f"解析完成：父级分块 {len(parent_docs)} 个，叶子分块 {len(leaf_docs)} 个",
        )

        failed_step = "parent_store"
        upload_job_manager.update_step(
            job_id, "parent_store", 20, "running", "正在写入父级分块"
        )
        parent_chunk_store.upsert_documents(parent_docs)
        upload_job_manager.complete_step(
            job_id, "parent_store", f"父级分块已入库：{len(parent_docs)} 个"
        )

        failed_step = "vector_store"
        total_leaf = len(leaf_docs)
        upload_job_manager.update_step(
            job_id,
            "vector_store",
            0,
            "running",
            f"正在向量化入库：0 / {total_leaf}",
            total_chunks=total_leaf,
            processed_chunks=0,
        )

        def _on_vector_progress(processed: int, total: int) -> None:
            percent = round(processed * 100 / total) if total else 100
            upload_job_manager.update_step(
                job_id,
                "vector_store",
                percent,
                "running",
                f"正在向量化入库：{processed} / {total}",
                total_chunks=total,
                processed_chunks=processed,
            )

        milvus_writer.write_documents(leaf_docs, progress_callback=_on_vector_progress)
        upload_job_manager.complete_step(
            job_id, "vector_store", f"向量化入库完成：{total_leaf} 个叶子分块"
        )
        upload_job_manager.complete_job(job_id, f"成功上传并处理 {filename}")
    except Exception:
        upload_job_manager.fail_job(job_id, failed_step, "文档处理失败，请查看服务日志")


def _process_delete_job(job_id: str, filename: str) -> None:
    try:
        chunks_deleted = delete_document_transactionally(
            filename, delete_job_manager, job_id
        )
        delete_job_manager.complete_job(
            job_id, f"已删除 {filename}，向量数据 {chunks_deleted} 条"
        )
    except Exception:
        job = delete_job_manager.get_job(job_id)
        current_step = job.get("current_step", "prepare") if job else "prepare"
        delete_job_manager.fail_job(
            job_id, current_step, "文档删除失败，请查看服务日志"
        )


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(_: User = Depends(require_admin)):
    try:
        milvus_manager.init_collection()
        results = milvus_manager.query(
            output_fields=["filename", "file_type"],
            limit=10000,
        )

        file_stats = {}
        for item in results:
            filename = item.get("filename", "")
            file_type = item.get("file_type", "")
            if filename not in file_stats:
                file_stats[filename] = {
                    "filename": filename,
                    "file_type": file_type,
                    "chunk_count": 0,
                }
            file_stats[filename]["chunk_count"] += 1

        documents = [DocumentInfo(**stats) for stats in file_stats.values()]
        return DocumentListResponse(documents=documents)
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "获取文档列表失败",
            status_code=503,
            retryable=True,
        ) from exc


@router.post("/documents/upload/async", response_model=DocumentUploadStartResponse)
async def upload_document_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    ensure_upload_dir()
    try:
        stored = await store_upload(file)
        filename = stored.original_name
        job = upload_job_manager.create_job(filename)
        upload_job_manager.update_step(
            job["job_id"], "upload", 1, "running", "正在保存文件到服务器"
        )
        upload_job_manager.complete_step(
            job["job_id"], "upload", "文件已上传，等待后台处理"
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "文件保存失败",
            status_code=503,
            retryable=True,
        ) from exc

    background_tasks.add_task(
        _process_upload_job, job["job_id"], str(stored.path), filename
    )
    return DocumentUploadStartResponse(
        job_id=job["job_id"],
        filename=filename,
        message="文件已上传，正在后台解析和向量化入库",
    )


@router.get("/documents/upload/jobs/{job_id}", response_model=DocumentUploadJobResponse)
async def get_upload_job(job_id: str, _: User = Depends(require_admin)):
    job = upload_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="上传任务不存在或已过期")
    return DocumentUploadJobResponse(**job)


@router.get("/documents/upload/jobs", response_model=list[DocumentUploadJobResponse])
async def list_upload_jobs(_: User = Depends(require_admin)):
    jobs = upload_job_manager.list_jobs()
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return [DocumentUploadJobResponse(**job) for job in jobs]


@router.delete(
    "/documents/delete/async/{filename}", response_model=DocumentDeleteStartResponse
)
async def delete_document_async(
    filename: str,
    background_tasks: BackgroundTasks,
    _: User = Depends(require_admin),
):
    job = delete_job_manager.create_job(
        filename,
        steps=DELETE_STEPS,
        current_step="prepare",
        message="等待删除",
        completion_step="parent_store",
    )
    delete_job_manager.update_step(
        job["job_id"], "prepare", 1, "running", "删除任务已提交"
    )
    background_tasks.add_task(_process_delete_job, job["job_id"], filename)
    return DocumentDeleteStartResponse(
        job_id=job["job_id"],
        filename=filename,
        message=f"正在删除 {filename}",
    )


@router.get("/documents/delete/jobs/{job_id}", response_model=DocumentDeleteJobResponse)
async def get_delete_job(job_id: str, _: User = Depends(require_admin)):
    job = delete_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="删除任务不存在或已过期")
    return DocumentDeleteJobResponse(**job)


@router.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...), _: User = Depends(require_admin)
):
    try:
        ensure_upload_dir()
        stored = await store_upload(file)
        filename = stored.original_name

        # Cleanup existing同名文档以保证一致性
        delete_document_transactionally(filename)

        try:
            new_docs = loader.load_document(str(stored.path), filename)
        except Exception as exc:
            raise AppError(
                ErrorCode.DOCUMENT_PARSE_FAILED,
                "文档处理失败",
                status_code=422,
            ) from exc

        if not new_docs:
            raise AppError(
                ErrorCode.DOCUMENT_PARSE_FAILED,
                "文档处理失败，未能提取内容",
                status_code=422,
            )

        parent_docs = [
            doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) in (1, 2)
        ]
        leaf_docs = [
            doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) == 3
        ]
        if not leaf_docs:
            raise AppError(
                ErrorCode.DOCUMENT_PARSE_FAILED,
                "文档处理失败，未生成可检索叶子分块",
                status_code=422,
            )

        parent_chunk_store.upsert_documents(parent_docs)
        milvus_writer.write_documents(leaf_docs)

        return DocumentUploadResponse(
            filename=filename,
            chunks_processed=len(leaf_docs),
            message=(
                f"成功上传并处理 {filename}，叶子分块 {len(leaf_docs)} 个，"
                f"父级分块 {len(parent_docs)} 个（存入 PostgreSQL）"
            ),
        )
    except (HTTPException, AppError):
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "文档上传失败",
            status_code=503,
            retryable=True,
        ) from exc


@router.delete("/documents/{filename}", response_model=DocumentDeleteResponse)
async def delete_document(filename: str, _: User = Depends(require_admin)):
    try:
        chunks_deleted = delete_document_transactionally(filename)

        return DocumentDeleteResponse(
            filename=filename,
            chunks_deleted=chunks_deleted,
            message=f"成功删除文档 {filename} 的向量数据（本地文件已保留）",
        )
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "删除文档失败",
            status_code=503,
            retryable=True,
        ) from exc
