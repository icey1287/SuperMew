from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import get_settings
from backend.documents.catalog import (
    BuildProfile,
    DocumentCatalog,
    DocumentRecord,
    DocumentVersionRecord,
    IndexJobRecord,
    IndexJobStatus,
    ManifestEntry,
    PublicationResult,
    StorageLayout,
    UploadReservation,
)
from backend.indexing.document_loader import DocumentArtifactMetadata, DocumentLoader
from backend.indexing.milvus_client import MilvusSettings
from backend.indexing.milvus_writer import (
    CATALOG_COLLECTION_SUFFIX,
    IndexVersionScope,
    MilvusWriter,
)
from backend.indexing.parent_chunk_store import ParentChunkStore
from backend.security.milvus_filters import eq_filter
from backend.security.uploads import StoredUpload


logger = logging.getLogger(__name__)

UPLOAD_STEPS: tuple[tuple[str, str], ...] = (
    ("upload", "文档上传"),
    ("reserve", "候选版本准备"),
    ("parse", "解析与版本化分块"),
    ("parent_store", "候选父级分块写入"),
    ("vector_store", "候选向量写入"),
    ("verify", "索引一致性核验"),
    ("publish", "原子发布新版本"),
)
_STEP_ORDER = tuple(key for key, _label in UPLOAD_STEPS)
_STEP_LABELS = dict(UPLOAD_STEPS)
_STEP_GLOBAL_PROGRESS = {
    "upload": 5,
    "reserve": 10,
    "parse": 30,
    "parent_store": 45,
    "vector_store": 80,
    "verify": 95,
    "publish": 100,
}
_TERMINAL_JOB_STATUSES = {
    IndexJobStatus.COMPLETED,
    IndexJobStatus.FAILED,
    IndexJobStatus.CANCELLED,
    IndexJobStatus.DEAD_LETTER,
}


ProgressCallback = Callable[[str, int, str], None]


@dataclass(frozen=True, slots=True)
class DocumentPublicationConfig:
    tenant_id: str
    knowledge_base_name: str
    parser_version: str
    chunker_version: str
    embedding_model: str
    index_version: str
    vector_collection: str
    upload_dir: Path
    max_attempts: int
    cleanup_grace: timedelta

    @classmethod
    def from_runtime(cls) -> DocumentPublicationConfig:
        settings = get_settings()
        base_collection = MilvusSettings.from_env().collection_name
        embedding_identity = (
            f"{settings.embedding.model}@{settings.embedding.revision}"
        )[:160]
        try:
            cleanup_seconds = max(
                int(os.getenv("DOCUMENT_INDEX_CLEANUP_GRACE_SECONDS", "3600")),
                0,
            )
        except ValueError:
            cleanup_seconds = 3600
        return cls(
            tenant_id=os.getenv("DEFAULT_TENANT_ID", "default").strip() or "default",
            knowledge_base_name=(
                os.getenv("DEFAULT_KNOWLEDGE_BASE_NAME", "默认知识库").strip()
                or "默认知识库"
            ),
            parser_version=(
                os.getenv("DOCUMENT_PARSER_VERSION", "document-loader-v2").strip()
                or "document-loader-v2"
            ),
            chunker_version=(
                os.getenv("DOCUMENT_CHUNKER_VERSION", "three-level-800-100-v2").strip()
                or "three-level-800-100-v2"
            ),
            embedding_model=embedding_identity,
            index_version=(
                os.getenv("DOCUMENT_INDEX_VERSION", "catalog-v1").strip()
                or "catalog-v1"
            ),
            vector_collection=f"{base_collection}{CATALOG_COLLECTION_SUFFIX}",
            upload_dir=settings.storage.upload_dir,
            max_attempts=settings.worker.max_attempts,
            cleanup_grace=timedelta(seconds=cleanup_seconds),
        )

    @property
    def build_profile(self) -> BuildProfile:
        return BuildProfile(
            parser_version=self.parser_version,
            chunker_version=self.chunker_version,
            embedding_model=self.embedding_model,
            index_version=self.index_version,
        )


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    job_id: str
    document: DocumentRecord
    version: DocumentVersionRecord
    previous_version: DocumentVersionRecord | None
    parent_chunk_count: int
    vector_chunk_count: int
    published: bool
    reused_current: bool = False


@dataclass(frozen=True, slots=True)
class DocumentRetirementOutcome:
    document_id: str
    canonical_name: str
    chunks_deleted: int
    cleanup_pending: bool = False
    cleanup_error_code: str | None = None
    cleanup_step: str | None = None


class _PreserveCandidateArtifacts(AppError):
    """Durable outcome is uncertain; candidate stores and source must stay intact."""


def _safe_error_code(exc: BaseException, *, stage: str) -> str:
    public_error = getattr(exc, "public_error", None)
    code = getattr(public_error, "code", None)
    if code:
        return str(code)
    if stage == "parse":
        return ErrorCode.DOCUMENT_PARSE_FAILED.value
    if stage in {"vector_store", "verify"}:
        return ErrorCode.VECTOR_STORE_UNAVAILABLE.value
    return ErrorCode.STORAGE_UNAVAILABLE.value


def _source_path(upload_dir: Path, object_key: str) -> Path:
    root = upload_dir.resolve()
    path = (root / object_key).resolve()
    if path.parent != root:
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "文档对象路径无效",
            status_code=503,
        )
    if not path.is_file():
        raise AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "待处理文档对象不存在",
            status_code=503,
            retryable=True,
        )
    return path


class DocumentPublication:
    """隐藏跨 Catalog、ParentChunk 与 Milvus 的两阶段发布 Implementation。"""

    def __init__(
        self,
        *,
        catalog: DocumentCatalog | None = None,
        loader: DocumentLoader | None = None,
        parent_store: ParentChunkStore | None = None,
        writer: MilvusWriter | None = None,
        config: DocumentPublicationConfig | None = None,
    ) -> None:
        self.catalog = catalog or DocumentCatalog()
        self.loader = loader or DocumentLoader()
        self.parent_store = parent_store or ParentChunkStore()
        self.writer = writer or MilvusWriter()
        self.config = config or DocumentPublicationConfig.from_runtime()
        self._job_locks_guard = Lock()
        self._job_locks: dict[str, Lock] = {}

    def submit(self, stored_upload: StoredUpload, owner_id: int) -> UploadReservation:
        try:
            knowledge_base = self.catalog.ensure_knowledge_base(
                tenant_id=self.config.tenant_id,
                owner_id=owner_id,
                name=self.config.knowledge_base_name,
            )
            reservation = self.catalog.reserve_upload(
                tenant_id=self.config.tenant_id,
                knowledge_base_id=knowledge_base.id,
                canonical_name=stored_upload.original_name,
                owner_id=owner_id,
                content_sha256=stored_upload.content_sha256,
                source_object_key=stored_upload.object_key,
                media_type=stored_upload.media_type,
                size_bytes=stored_upload.size_bytes,
                processing_profile=self.config.build_profile,
                vector_collection=self.config.vector_collection,
                max_attempts=self.config.max_attempts,
                cleanup_grace=self.config.cleanup_grace,
            )
        except Exception:
            self._discard_upload(stored_upload)
            raise
        if reservation.version.source_object_key != stored_upload.object_key:
            self._discard_upload(stored_upload)
        if not reservation.already_current and (
            reservation.created or reservation.requeued
        ):
            self._update_job(
                reservation.job.id,
                reservation.publication_fence,
                status=IndexJobStatus.PENDING,
                current_step="upload",
                progress=_STEP_GLOBAL_PROGRESS["upload"],
                message="文件已保存，候选版本等待构建",
                step_percent=100,
            )
        return reservation

    def _discard_upload(self, stored_upload: StoredUpload) -> None:
        root = self.config.upload_dir.resolve()
        path = stored_upload.path.resolve()
        if path.parent == root:
            with suppress(OSError):
                path.unlink(missing_ok=True)

    def _update_job(
        self,
        job_id: str,
        publication_fence: int,
        *,
        status: str,
        current_step: str,
        progress: int,
        message: str,
        step_percent: int,
        increment_attempts: bool = False,
        total_chunks: int | None = None,
        processed_chunks: int | None = None,
    ) -> IndexJobRecord:
        patch: dict[str, Any] = {
            "message": message,
            "active_step": current_step,
            "active_step_percent": max(0, min(100, int(step_percent))),
        }
        if total_chunks is not None:
            patch["total_chunks"] = max(int(total_chunks), 0)
        if processed_chunks is not None:
            patch["processed_chunks"] = max(int(processed_chunks), 0)
        return self.catalog.update_job(
            job_id=job_id,
            publication_fence=publication_fence,
            status=status,
            current_step=current_step,
            progress=max(0, min(100, int(progress))),
            step_state_patch=patch,
            increment_attempts=increment_attempts,
        )

    @staticmethod
    def _notify(
        callback: ProgressCallback | None,
        step: str,
        percent: int,
        message: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(step, percent, message)
        except Exception:
            logger.warning(
                "document publication progress callback failed", extra={"step": step}
            )

    def _step(
        self,
        *,
        job_id: str,
        publication_fence: int,
        step: str,
        global_progress: int,
        step_percent: int,
        message: str,
        callback: ProgressCallback | None,
        increment_attempts: bool = False,
        total_chunks: int | None = None,
        processed_chunks: int | None = None,
        status: str = IndexJobStatus.RUNNING,
    ) -> None:
        self._update_job(
            job_id,
            publication_fence,
            status=status,
            current_step=step,
            progress=global_progress,
            message=message,
            step_percent=step_percent,
            increment_attempts=increment_attempts,
            total_chunks=total_chunks,
            processed_chunks=processed_chunks,
        )
        self._notify(callback, step, step_percent, message)

    @staticmethod
    def _manifest(documents: list[dict], store_kind: str) -> list[ManifestEntry]:
        return [
            ManifestEntry(
                chunk_id=str(document["chunk_id"]),
                content_hash=str(document["content_hash"]),
                store_kind=store_kind,
                section_id=str(document.get("section_id", "")),
                chunk_level=int(document.get("chunk_level", 0) or 0),
            )
            for document in documents
        ]

    def run(
        self,
        reservation_or_job_id: UploadReservation | str,
        progress: ProgressCallback | None = None,
    ) -> PublicationOutcome:
        job_id = (
            reservation_or_job_id.job.id
            if isinstance(reservation_or_job_id, UploadReservation)
            else str(reservation_or_job_id)
        )
        with self._job_locks_guard:
            job_lock = self._job_locks.setdefault(job_id, Lock())
        with job_lock:
            return self._run_once(job_id, progress)

    def _run_once(
        self,
        job_id: str,
        progress: ProgressCallback | None,
    ) -> PublicationOutcome:
        build = self.catalog.load_build(job_id=job_id, tenant_id=self.config.tenant_id)
        if build.job.status == IndexJobStatus.COMPLETED:
            return PublicationOutcome(
                job_id=job_id,
                document=build.document,
                version=build.version,
                previous_version=None,
                parent_chunk_count=build.version.parent_chunk_count,
                vector_chunk_count=build.version.chunk_count,
                published=False,
                reused_current=True,
            )
        if build.job.status == IndexJobStatus.STAGED:
            return self._publish_staged(build, progress)
        if build.job.status in {
            IndexJobStatus.FAILED,
            IndexJobStatus.CANCELLED,
            IndexJobStatus.DEAD_LETTER,
        }:
            raise AppError(
                ErrorCode.CONFLICT,
                "索引任务已失效，不能继续构建",
                status_code=409,
            )

        fence = build.job.publication_fence
        scope = self.writer.build_version_scope(
            tenant_id=build.document.tenant_id,
            knowledge_base_id=build.document.knowledge_base_id,
            document_id=build.document.id,
            document_version_id=build.version.id,
            index_version=build.version.index_version,
            collection_name=build.version.vector_collection,
        )
        stage = "reserve"
        published = False
        progress_floor = max(int(build.job.progress or 0), 0)
        parent_documents: list[dict] = []
        leaf_documents: list[dict] = []
        try:
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step="reserve",
                global_progress=max(progress_floor, _STEP_GLOBAL_PROGRESS["reserve"]),
                step_percent=100,
                message=f"候选版本 v{build.version.version_number} 已获得发布 fencing",
                callback=progress,
                increment_attempts=True,
            )

            stage = "parse"
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step=stage,
                global_progress=max(progress_floor, 12),
                step_percent=5,
                message="正在解析并生成版本隔离的三级分块",
                callback=progress,
            )
            source_path = _source_path(
                self.config.upload_dir,
                build.version.source_object_key,
            )
            try:
                documents = self.loader.load_document(
                    str(source_path),
                    build.document.canonical_name,
                    metadata=DocumentArtifactMetadata(
                        tenant_id=build.document.tenant_id,
                        knowledge_base_id=build.document.knowledge_base_id,
                        document_id=build.document.id,
                        document_version_id=build.version.id,
                        index_version=build.version.index_version,
                    ),
                )
            except AppError:
                raise
            except Exception as exc:
                raise AppError(
                    ErrorCode.DOCUMENT_PARSE_FAILED,
                    "文档处理失败",
                    status_code=422,
                ) from exc
            parent_documents = [
                document
                for document in documents
                if int(document.get("chunk_level", 0) or 0) in {1, 2}
            ]
            leaf_documents = [
                document
                for document in documents
                if int(document.get("chunk_level", 0) or 0) == 3
            ]
            if not leaf_documents:
                raise AppError(
                    ErrorCode.DOCUMENT_PARSE_FAILED,
                    "文档处理失败，未生成可检索叶子分块",
                    status_code=422,
                )
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step=stage,
                global_progress=max(progress_floor, _STEP_GLOBAL_PROGRESS[stage]),
                step_percent=100,
                message=(
                    f"版本化分块完成：父级 {len(parent_documents)}，"
                    f"叶子 {len(leaf_documents)}"
                ),
                callback=progress,
                total_chunks=len(leaf_documents),
                processed_chunks=0,
            )

            stage = "parent_store"
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step=stage,
                global_progress=max(progress_floor, 32),
                step_percent=10,
                message="正在写入隔离的候选父级分块",
                callback=progress,
            )
            # A crashed attempt may have left a strict subset/superset behind. The
            # candidate is unpublished, so replacing this exact version scope makes
            # the next attempt idempotent without touching the current version.
            self.parent_store.delete_by_version(
                tenant_id=scope.tenant_id,
                knowledge_base_id=scope.knowledge_base_id,
                document_id=scope.document_id,
                document_version_id=scope.document_version_id,
                index_version=scope.index_version,
            )
            parent_count = self.parent_store.upsert_documents(parent_documents)
            if parent_count != len(parent_documents):
                raise RuntimeError("parent chunk write count mismatch")
            parent_verification = self.parent_store.verify_version(
                tenant_id=scope.tenant_id,
                knowledge_base_id=scope.knowledge_base_id,
                document_id=scope.document_id,
                document_version_id=scope.document_version_id,
                index_version=scope.index_version,
                expected_chunk_ids=[
                    str(document["chunk_id"]) for document in parent_documents
                ],
            )
            if not parent_verification.exact:
                raise RuntimeError("parent chunk exact verification failed")
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step=stage,
                global_progress=max(progress_floor, _STEP_GLOBAL_PROGRESS[stage]),
                step_percent=100,
                message=f"候选父级分块精确核验通过：{parent_count} 条",
                callback=progress,
            )

            stage = "vector_store"
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step=stage,
                global_progress=max(progress_floor, 46),
                step_percent=1,
                message=f"正在写入候选向量：0 / {len(leaf_documents)}",
                callback=progress,
                total_chunks=len(leaf_documents),
                processed_chunks=0,
            )

            def on_vector_progress(processed: int, total: int) -> None:
                local_percent = round(processed * 100 / total) if total else 100
                global_progress = max(
                    progress_floor,
                    46 + round(local_percent * 0.34),
                )
                self._step(
                    job_id=job_id,
                    publication_fence=fence,
                    step="vector_store",
                    global_progress=min(
                        global_progress, _STEP_GLOBAL_PROGRESS["vector_store"]
                    ),
                    step_percent=local_percent,
                    message=f"正在写入候选向量：{processed} / {total}",
                    callback=progress,
                    total_chunks=total,
                    processed_chunks=processed,
                )

            receipt = self.writer.write_versioned_documents(
                leaf_documents,
                collection_name=scope.collection_name,
                progress_callback=on_vector_progress,
            )

            stage = "verify"
            self._step(
                job_id=job_id,
                publication_fence=fence,
                step=stage,
                global_progress=max(progress_floor, 85),
                step_percent=25,
                message="正在核验 Parent、Milvus 与 manifest 的精确身份",
                callback=progress,
                total_chunks=len(leaf_documents),
                processed_chunks=len(leaf_documents),
            )
            vector_verification = self.writer.verify_receipt(receipt)
            if not vector_verification.exact:
                raise RuntimeError("Milvus exact verification failed")
            manifest = [
                *self._manifest(parent_documents, "parent"),
                *self._manifest(leaf_documents, "vector"),
            ]
            self.catalog.record_manifest(
                job_id=job_id,
                publication_fence=fence,
                entries=manifest,
                vector_chunk_count=len(leaf_documents),
                parent_chunk_count=len(parent_documents),
            )
            self._notify(progress, stage, 100, "跨存储索引一致性核验通过")

            stage = "publish"
            self._notify(progress, stage, 20, "正在原子切换 PostgreSQL current_version")
            result = self._publish_with_reconciliation(build)
            published = True
            self._notify(progress, stage, 100, "新版本已原子发布，旧版本进入延迟清理")
            return PublicationOutcome(
                job_id=job_id,
                document=result.document,
                version=result.version,
                previous_version=result.previous_version,
                parent_chunk_count=len(parent_documents),
                vector_chunk_count=len(leaf_documents),
                published=result.published,
            )
        except Exception as exc:
            if isinstance(exc, _PreserveCandidateArtifacts):
                raise
            if stage == "verify" and self._staged_candidate_is_durable(
                job_id,
                build.version.id,
            ):
                raise _PreserveCandidateArtifacts(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "候选 manifest 已持久化，版本已保留以便安全发布",
                    status_code=503,
                    retryable=True,
                    stage="verify",
                ) from exc
            if not published:
                self._record_failure_before_cleanup(
                    build=build,
                    stage=stage,
                    error=exc,
                )
                cleaned = self._cleanup_candidate(scope, build.version)
                if cleaned:
                    with suppress(Exception):
                        self.catalog.record_cleanup(
                            document_version_id=scope.document_version_id
                        )
            if isinstance(exc, AppError):
                raise
            raise AppError(
                _safe_error_code(exc, stage=stage),
                "文档候选版本构建失败，旧版本仍保持可用",
                status_code=503,
                retryable=stage not in {"parse", "publish"},
                stage=stage,
            ) from exc

    def _staged_candidate_is_durable(self, job_id: str, version_id: str) -> bool:
        try:
            refreshed = self.catalog.load_build(
                job_id=job_id,
                tenant_id=self.config.tenant_id,
            )
        except Exception:
            # Unknown commit state is resolved by preserving the unpublished
            # candidate. A later retry can safely rebuild or publish it.
            return True
        pending = refreshed.document.pending_version
        return bool(
            refreshed.job.status == IndexJobStatus.STAGED
            and pending is not None
            and pending.id == version_id
        )

    def _publish_with_reconciliation(self, build) -> PublicationResult:
        try:
            return self.catalog.publish(
                job_id=build.job.id,
                publication_fence=build.job.publication_fence,
                expected_current_version_id=build.job.expected_current_version_id,
                cleanup_grace=self.config.cleanup_grace,
            )
        except Exception as exc:
            try:
                refreshed = self.catalog.load_build(
                    job_id=build.job.id,
                    tenant_id=self.config.tenant_id,
                )
            except Exception as refresh_exc:
                raise _PreserveCandidateArtifacts(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "发布结果暂时无法确认，候选版本已保留以便安全重试",
                    status_code=503,
                    retryable=True,
                    stage="publish",
                ) from refresh_exc
            current = refreshed.document.current_version
            if (
                refreshed.job.status == IndexJobStatus.COMPLETED
                and current is not None
                and current.id == refreshed.version.id
            ):
                previous = build.document.current_version
                return PublicationResult(
                    document=refreshed.document,
                    version=refreshed.version,
                    previous_version=(
                        previous
                        if previous is not None and previous.id != refreshed.version.id
                        else None
                    ),
                    published=True,
                )
            pending = refreshed.document.pending_version
            if (
                refreshed.job.status == IndexJobStatus.STAGED
                and pending is not None
                and pending.id == refreshed.version.id
            ):
                raise _PreserveCandidateArtifacts(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "候选版本已完成核验，原子发布将在后续重试",
                    status_code=503,
                    retryable=True,
                    stage="publish",
                ) from exc
            raise

    def _publish_staged(
        self,
        build,
        progress: ProgressCallback | None,
    ) -> PublicationOutcome:
        scope = self.writer.build_version_scope(
            tenant_id=build.document.tenant_id,
            knowledge_base_id=build.document.knowledge_base_id,
            document_id=build.document.id,
            document_version_id=build.version.id,
            index_version=build.version.index_version,
            collection_name=build.version.vector_collection,
        )
        try:
            self._notify(
                progress,
                "publish",
                20,
                "已恢复 exact-verified 候选，正在原子发布",
            )
            result = self._publish_with_reconciliation(build)
            self._notify(progress, "publish", 100, "候选版本恢复后已原子发布")
            return PublicationOutcome(
                job_id=build.job.id,
                document=result.document,
                version=result.version,
                previous_version=result.previous_version,
                parent_chunk_count=result.version.parent_chunk_count,
                vector_chunk_count=result.version.chunk_count,
                published=result.published,
            )
        except Exception as exc:
            if isinstance(exc, _PreserveCandidateArtifacts):
                raise
            self._record_failure_before_cleanup(
                build=build,
                stage="publish",
                error=exc,
            )
            cleaned = self._cleanup_candidate(scope, build.version)
            if cleaned:
                with suppress(Exception):
                    self.catalog.record_cleanup(
                        document_version_id=scope.document_version_id
                    )
            if isinstance(exc, AppError):
                raise
            raise AppError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "候选版本恢复发布失败，旧版本仍保持可用",
                status_code=503,
                stage="publish",
            ) from exc

    def _record_failure_before_cleanup(
        self,
        *,
        build,
        stage: str,
        error: Exception,
    ) -> None:
        try:
            self.catalog.fail(
                job_id=build.job.id,
                publication_fence=build.job.publication_fence,
                error_code=_safe_error_code(error, stage=stage),
                error_detail_redacted=f"stage={stage}",
            )
            return
        except Exception as transition_error:
            if self._candidate_is_terminal_and_unreferenced(
                build.job.id,
                build.version.id,
            ):
                return
            raise _PreserveCandidateArtifacts(
                ErrorCode.STORAGE_UNAVAILABLE,
                "候选失败状态暂时无法确认，构建产物已保留以便安全恢复",
                status_code=503,
                retryable=True,
                stage=stage,
            ) from transition_error

    def _candidate_is_terminal_and_unreferenced(
        self,
        job_id: str,
        version_id: str,
    ) -> bool:
        try:
            refreshed = self.catalog.load_build(
                job_id=job_id,
                tenant_id=self.config.tenant_id,
            )
        except Exception:
            return False
        current = refreshed.document.current_version
        pending = refreshed.document.pending_version
        return bool(
            refreshed.version.id == version_id
            and refreshed.job.status
            in {
                IndexJobStatus.FAILED,
                IndexJobStatus.CANCELLED,
                IndexJobStatus.DEAD_LETTER,
            }
            and refreshed.version.status
            in {
                "failed",
                "superseded",
            }
            and (current is None or current.id != version_id)
            and (pending is None or pending.id != version_id)
        )

    def _cleanup_candidate(
        self,
        scope: IndexVersionScope,
        version: DocumentVersionRecord,
    ) -> bool:
        cleaned = True
        try:
            self.writer.delete_by_version(scope)
        except Exception:
            cleaned = False
        try:
            self.parent_store.delete_by_version(
                tenant_id=scope.tenant_id,
                knowledge_base_id=scope.knowledge_base_id,
                document_id=scope.document_id,
                document_version_id=scope.document_version_id,
                index_version=scope.index_version,
            )
        except Exception:
            cleaned = False
        try:
            self._unlink_version_object(version)
        except Exception:
            cleaned = False
        return cleaned

    def cleanup_version(
        self,
        *,
        document: DocumentRecord,
        version: DocumentVersionRecord,
    ) -> int:
        deleted_vectors = 0
        if version.storage_layout == StorageLayout.VERSIONED:
            scope = self.writer.build_version_scope(
                tenant_id=document.tenant_id,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                document_version_id=version.id,
                index_version=version.index_version,
                collection_name=version.vector_collection,
            )
            try:
                deleted_vectors = self.writer.delete_by_version(scope)
            except Exception as exc:
                raise AppError(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "文档向量索引清理暂时失败",
                    status_code=503,
                    retryable=True,
                    stage="milvus",
                ) from exc
            try:
                self.parent_store.delete_by_version(
                    tenant_id=scope.tenant_id,
                    knowledge_base_id=scope.knowledge_base_id,
                    document_id=scope.document_id,
                    document_version_id=scope.document_version_id,
                    index_version=scope.index_version,
                )
            except Exception as exc:
                raise AppError(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "文档父级分块清理暂时失败",
                    status_code=503,
                    retryable=True,
                    stage="parent_store",
                ) from exc
            try:
                self._unlink_version_object(version)
            except Exception as exc:
                raise AppError(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "文档版本对象清理暂时失败",
                    status_code=503,
                    retryable=True,
                    stage="object_store",
                ) from exc
        elif version.storage_layout == StorageLayout.LEGACY_FILENAME:
            filename = document.canonical_name
            store = self.writer.milvus_manager.with_collection(
                version.vector_collection or self.writer.milvus_manager.collection_name
            )
            try:
                result = store.delete(eq_filter("filename", filename))
            except Exception as exc:
                raise AppError(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "legacy 文档向量清理暂时失败",
                    status_code=503,
                    retryable=True,
                    stage="milvus",
                ) from exc
            if isinstance(result, dict):
                deleted_vectors = int(result.get("delete_count", 0) or 0)
            try:
                self.parent_store.delete_by_filename(filename)
            except Exception as exc:
                raise AppError(
                    ErrorCode.STORAGE_UNAVAILABLE,
                    "legacy 父级分块清理暂时失败",
                    status_code=503,
                    retryable=True,
                    stage="parent_store",
                ) from exc
        else:
            raise AppError(
                ErrorCode.CONFLICT,
                "未知文档存储布局，无法清理",
                status_code=409,
            )
        try:
            self.catalog.record_cleanup(document_version_id=version.id)
        except Exception as exc:
            raise AppError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "文档清理结果暂时无法持久化",
                status_code=503,
                retryable=True,
                stage="finalize",
            ) from exc
        return deleted_vectors

    def _unlink_version_object(self, version: DocumentVersionRecord) -> None:
        root = self.config.upload_dir.resolve()
        path = (root / version.source_object_key).resolve()
        if path.parent != root:
            raise AppError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "文档对象清理路径无效",
                status_code=503,
            )
        path.unlink(missing_ok=True)

    def retire(
        self,
        canonical_name: str,
        *,
        owner_id: int | None = None,
    ) -> DocumentRetirementOutcome:
        document = self._find_document(canonical_name)
        result = self.catalog.retire(
            tenant_id=self.config.tenant_id,
            canonical_name=canonical_name,
            knowledge_base_id=(document.knowledge_base_id if document else None),
            cleanup_grace=timedelta(0),
        )
        knowledge_base_id = result.knowledge_base_id
        if knowledge_base_id is None:
            knowledge_base = self.catalog.find_knowledge_base(
                tenant_id=self.config.tenant_id,
                name=self.config.knowledge_base_name,
            )
            if knowledge_base is None:
                if owner_id is None:
                    raise AppError(
                        ErrorCode.INVALID_REQUEST,
                        "删除未接管 legacy 文档时必须提供 owner_id",
                        status_code=400,
                    )
                knowledge_base = self.catalog.ensure_knowledge_base(
                    tenant_id=self.config.tenant_id,
                    owner_id=owner_id,
                    name=self.config.knowledge_base_name,
                )
            knowledge_base_id = knowledge_base.id
        tombstone_owner_id = document.owner_id if document else owner_id
        if tombstone_owner_id is None:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "删除未接管 legacy 文档时必须提供 owner_id",
                status_code=400,
            )
        try:
            tombstone = self.catalog.suppress_legacy_name(
                tenant_id=self.config.tenant_id,
                knowledge_base_id=knowledge_base_id,
                canonical_name=canonical_name,
                owner_id=tombstone_owner_id,
                vector_collection=self.writer.milvus_manager.collection_name,
                cleanup_grace=timedelta(0),
            )
        except Exception as exc:
            raise AppError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "legacy 删除封印暂时无法持久化，请重试删除",
                status_code=503,
                retryable=True,
                safe_details={
                    "logical_retired": False,
                    "catalog_scope_revoked": bool(result.found),
                },
                stage="catalog_tombstone",
            ) from exc
        cleanup_versions = {
            version.id: version
            for version in (*result.cleanup_versions, *tombstone.cleanup_versions)
        }
        document = self._find_document(canonical_name)
        if document is None:
            raise AppError(
                ErrorCode.CONFLICT,
                "目录删除结果缺少文档 scope",
                status_code=409,
            )
        deleted = 0
        cleanup_error_code: str | None = None
        cleanup_step: str | None = None
        for version in cleanup_versions.values():
            try:
                deleted += self.cleanup_version(document=document, version=version)
            except Exception as exc:
                public_error = getattr(exc, "public_error", None)
                error_code = _safe_error_code(exc, stage="cleanup")
                failed_step = str(getattr(public_error, "stage", None) or "finalize")
                with suppress(Exception):
                    self.catalog.record_cleanup(
                        document_version_id=version.id,
                        error_code=error_code,
                    )
                cleanup_error_code = cleanup_error_code or error_code
                cleanup_step = cleanup_step or failed_step
                logger.warning(
                    "retired document cleanup deferred",
                    extra={
                        "document_id": document.id,
                        "document_version_id": version.id,
                        "cleanup_step": failed_step,
                        "error_code": error_code,
                    },
                )
        return DocumentRetirementOutcome(
            document_id=document.id,
            canonical_name=document.canonical_name,
            chunks_deleted=deleted,
            cleanup_pending=cleanup_error_code is not None,
            cleanup_error_code=cleanup_error_code,
            cleanup_step=cleanup_step,
        )

    def _find_document(self, canonical_name: str) -> DocumentRecord | None:
        offset = 0
        while True:
            documents = self.catalog.list_documents(
                tenant_id=self.config.tenant_id,
                include_deleted=True,
                limit=1000,
                offset=offset,
            )
            match = next(
                (item for item in documents if item.canonical_name == canonical_name),
                None,
            )
            if match is not None or len(documents) < 1000:
                return match
            offset += len(documents)

    def get_job_view(self, job_id: str) -> dict[str, Any]:
        return index_job_compatibility_view(
            self.catalog.get_job(job_id=job_id, tenant_id=self.config.tenant_id)
        )

    def list_job_views(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            index_job_compatibility_view(job)
            for job in self.catalog.list_jobs(
                tenant_id=self.config.tenant_id,
                limit=limit,
                offset=0,
            )
        ]


def _view_step_key(current_step: str) -> str:
    aliases = {
        "uploaded": "upload",
        "verified": "verify",
        "published": "publish",
        "legacy_adopted": "publish",
        "failed": "publish",
        "superseded": "publish",
    }
    normalized = aliases.get(current_step, current_step)
    return normalized if normalized in _STEP_ORDER else "reserve"


def index_job_compatibility_view(job: IndexJobRecord) -> dict[str, Any]:
    failed = job.status in {
        IndexJobStatus.FAILED,
        IndexJobStatus.CANCELLED,
        IndexJobStatus.DEAD_LETTER,
    }
    state = dict(job.step_state or {})
    active = _view_step_key(
        str(state.get("active_step") or job.current_step)
        if failed
        else job.current_step
    )
    active_index = _STEP_ORDER.index(active)
    active_percent = int(state.get("active_step_percent", 0) or 0)
    message = str(state.get("message") or "")
    steps: list[dict[str, Any]] = []
    for index, key in enumerate(_STEP_ORDER):
        if job.status == IndexJobStatus.COMPLETED:
            percent, status = 100, "completed"
        elif index < active_index:
            percent, status = 100, "completed"
        elif index == active_index:
            percent = max(0, min(100, active_percent))
            status = "failed" if failed else "running"
        else:
            percent, status = 0, "pending"
        steps.append(
            {
                "key": key,
                "label": _STEP_LABELS[key],
                "percent": percent,
                "status": status,
                "message": message if index == active_index else "",
            }
        )
    return {
        "job_id": job.id,
        "document_id": job.document_id,
        "document_version_id": job.document_version_id,
        "filename": job.canonical_name,
        "status": str(job.status),
        "current_step": active,
        "message": message
        or ("文档版本已发布" if job.status == IndexJobStatus.COMPLETED else "等待处理"),
        "total_chunks": int(state.get("total_chunks", 0) or 0),
        "processed_chunks": int(state.get("processed_chunks", 0) or 0),
        "error": job.error_code,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "steps": steps,
    }


__all__ = [
    "DocumentPublication",
    "DocumentPublicationConfig",
    "DocumentRetirementOutcome",
    "PublicationOutcome",
    "UPLOAD_STEPS",
    "index_job_compatibility_view",
]
