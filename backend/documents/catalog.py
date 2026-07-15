from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.errors import AppError, ErrorCode
from backend.db.models import (
    Document,
    DocumentCatalogState,
    DocumentVersion,
    IndexJob,
    IndexManifest,
    KnowledgeBase,
    utcnow,
)
from backend.infra.database import SessionLocal


SessionFactory = Callable[[], Session]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_CLEANUP_GRACE = timedelta(hours=1)
_LEGACY_TOMBSTONE_PARSER = "legacy-tombstone"


class StorageLayout(StrEnum):
    VERSIONED = "versioned"
    LEGACY_FILENAME = "legacy_filename"


class DocumentVersionStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    INDEXING = "indexing"
    STAGED = "staged"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


_ACTIVE_VERSION_STATUSES = {
    DocumentVersionStatus.UPLOADED,
    DocumentVersionStatus.PARSING,
    DocumentVersionStatus.INDEXING,
    DocumentVersionStatus.STAGED,
    DocumentVersionStatus.READY,
}


class IndexJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    STAGED = "staged"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


_JOB_TRANSITIONS: dict[str, set[str]] = {
    IndexJobStatus.PENDING: {
        IndexJobStatus.PENDING,
        IndexJobStatus.RUNNING,
        IndexJobStatus.RETRY_WAIT,
        IndexJobStatus.FAILED,
        IndexJobStatus.CANCELLED,
    },
    IndexJobStatus.RUNNING: {
        IndexJobStatus.RUNNING,
        IndexJobStatus.RETRY_WAIT,
        IndexJobStatus.STAGED,
        IndexJobStatus.FAILED,
        IndexJobStatus.CANCELLED,
        IndexJobStatus.DEAD_LETTER,
    },
    IndexJobStatus.RETRY_WAIT: {
        IndexJobStatus.RETRY_WAIT,
        IndexJobStatus.PENDING,
        IndexJobStatus.RUNNING,
        IndexJobStatus.FAILED,
        IndexJobStatus.CANCELLED,
        IndexJobStatus.DEAD_LETTER,
    },
    IndexJobStatus.STAGED: {
        IndexJobStatus.STAGED,
        IndexJobStatus.COMPLETED,
        IndexJobStatus.FAILED,
        IndexJobStatus.CANCELLED,
    },
    IndexJobStatus.COMPLETED: {IndexJobStatus.COMPLETED},
    IndexJobStatus.FAILED: {IndexJobStatus.FAILED},
    IndexJobStatus.CANCELLED: {IndexJobStatus.CANCELLED},
    IndexJobStatus.DEAD_LETTER: {IndexJobStatus.DEAD_LETTER},
}


@dataclass(frozen=True)
class BuildProfile:
    parser_version: str = "v1"
    chunker_version: str = "v1"
    embedding_model: str = ""
    index_version: str = "v1"

    @property
    def fingerprint(self) -> str:
        return _payload_hash(
            {
                "schema_version": 1,
                "parser_version": self.parser_version,
                "chunker_version": self.chunker_version,
                "embedding_model": self.embedding_model,
                "index_version": self.index_version,
            }
        )


@dataclass(frozen=True)
class ManifestEntry:
    chunk_id: str
    content_hash: str
    store_kind: str = "vector"
    section_id: str = ""
    chunk_level: int = 0


@dataclass(frozen=True)
class KnowledgeBaseRecord:
    id: str
    tenant_id: str
    name: str
    owner_id: int
    status: str
    catalog_revision: int


@dataclass(frozen=True)
class DocumentVersionRecord:
    id: str
    document_id: str
    version_number: int
    content_sha256: str
    build_fingerprint: str
    source_object_key: str
    media_type: str
    size_bytes: int
    parser_version: str
    chunker_version: str
    embedding_model: str
    index_version: str
    storage_layout: str
    vector_collection: str
    legacy_identity: str | None
    status: str
    chunk_count: int
    parent_chunk_count: int
    error_code: str | None
    published_at: datetime | None
    superseded_at: datetime | None
    cleanup_after: datetime | None
    index_cleaned_at: datetime | None
    cleanup_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IndexJobRecord:
    id: str
    document_id: str
    document_version_id: str
    canonical_name: str
    tenant_id: str
    status: str
    current_step: str
    progress: int
    attempts: int
    max_attempts: int
    publication_fence: int
    expected_current_version_id: str | None
    owner_worker_id: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    next_retry_at: datetime | None
    error_code: str | None
    step_state: dict
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DocumentRecord:
    id: str
    tenant_id: str
    knowledge_base_id: str
    canonical_name: str
    owner_id: int
    status: str
    publication_fence: int
    version_counter: int
    catalog_revision: int
    current_version: DocumentVersionRecord | None
    pending_version: DocumentVersionRecord | None
    pending_job: IndexJobRecord | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class UploadReservation:
    document: DocumentRecord
    version: DocumentVersionRecord
    job: IndexJobRecord
    created: bool
    requeued: bool
    already_current: bool
    publication_fence: int
    expected_current_version_id: str | None


@dataclass(frozen=True)
class VersionBuild:
    job: IndexJobRecord
    document: DocumentRecord
    version: DocumentVersionRecord


@dataclass(frozen=True)
class PublicationResult:
    document: DocumentRecord
    version: DocumentVersionRecord
    previous_version: DocumentVersionRecord | None
    published: bool


@dataclass(frozen=True)
class RetirementResult:
    document_id: str | None
    tenant_id: str
    knowledge_base_id: str | None
    canonical_name: str
    found: bool
    already_deleted: bool
    cleanup_versions: tuple[DocumentVersionRecord, ...]


@dataclass(frozen=True)
class CleanupCandidate:
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    canonical_name: str
    version: DocumentVersionRecord


@dataclass(frozen=True)
class RetrievalCatalogSnapshot:
    tenant_id: str
    knowledge_base_id: str | None
    documents: tuple[DocumentRecord, ...]
    index_id: str
    suppressed_legacy_names: tuple[str, ...]
    legacy_adoption_complete: bool
    legacy_corpus_fingerprint: str
    legacy_collection: str | None
    legacy_knowledge_base_id: str | None
    legacy_knowledge_base_name: str | None


@dataclass(frozen=True)
class LegacyAdoptionState:
    tenant_id: str
    legacy_collection: str | None
    knowledge_base_id: str | None
    knowledge_base_name: str | None
    state_exists: bool
    complete: bool
    fingerprint: str
    fence: int


@dataclass(frozen=True)
class LegacyAdoptionResult:
    document: DocumentRecord
    version: DocumentVersionRecord | None
    job: IndexJobRecord | None
    adopted: bool
    reason: str


def _payload_hash(payload: Mapping) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def legacy_source_identity(*, vector_collection: str, canonical_name: str) -> str:
    """Return the immutable authorization key for one filename-scoped corpus."""

    collection = _required_text(vector_collection, "vector_collection", 160)
    name = _required_text(canonical_name, "canonical_name", 255)
    return "legacy:source:v1:" + _payload_hash(
        {
            "schema_version": 1,
            "vector_collection": collection,
            "canonical_name": name,
        }
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _required_text(value: str, field: str, maximum: int) -> str:
    normalized = unicodedata.normalize("NFC", str(value or "")).strip()
    if not normalized or len(normalized) > maximum:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{field} 必须为 1-{maximum} 个字符",
            status_code=400,
        )
    return normalized


def _content_hash(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "content_sha256 必须是 64 位十六进制摘要",
            status_code=400,
        )
    return normalized


def _cleanup_at(now: datetime, grace: timedelta) -> datetime:
    if grace.total_seconds() < 0:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "cleanup_grace 不能为负数",
            status_code=400,
        )
    return now + grace


class DocumentCatalog:
    """文档目录、候选构建与原子发布的唯一持久化 Interface。"""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _knowledge_base_record(row: KnowledgeBase) -> KnowledgeBaseRecord:
        return KnowledgeBaseRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            owner_id=row.owner_id,
            status=row.status,
            catalog_revision=row.catalog_revision,
        )

    @staticmethod
    def _version_record(row: DocumentVersion) -> DocumentVersionRecord:
        return DocumentVersionRecord(
            id=row.id,
            document_id=row.document_id,
            version_number=row.version_number,
            content_sha256=row.content_sha256,
            build_fingerprint=row.build_fingerprint,
            source_object_key=row.source_object_key,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            parser_version=row.parser_version,
            chunker_version=row.chunker_version,
            embedding_model=row.embedding_model,
            index_version=row.index_version,
            storage_layout=row.storage_layout,
            vector_collection=row.vector_collection,
            legacy_identity=row.legacy_identity,
            status=row.status,
            chunk_count=row.chunk_count,
            parent_chunk_count=row.parent_chunk_count,
            error_code=row.error_code,
            published_at=row.published_at,
            superseded_at=row.superseded_at,
            cleanup_after=row.cleanup_after,
            index_cleaned_at=row.index_cleaned_at,
            cleanup_error_code=row.cleanup_error_code,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _job_record(
        row: IndexJob,
        *,
        document: Document,
    ) -> IndexJobRecord:
        return IndexJobRecord(
            id=row.id,
            document_id=document.id,
            document_version_id=row.document_version_id,
            canonical_name=document.canonical_name,
            tenant_id=document.tenant_id,
            status=row.status,
            current_step=row.current_step,
            progress=row.progress,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            publication_fence=row.publication_fence,
            expected_current_version_id=row.expected_current_version_id,
            owner_worker_id=row.owner_worker_id,
            lease_expires_at=row.lease_expires_at,
            heartbeat_at=row.heartbeat_at,
            next_retry_at=row.next_retry_at,
            error_code=row.error_code,
            step_state=dict(row.step_state_json or {}),
            finished_at=row.finished_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _bump_revision(knowledge_base: KnowledgeBase, now: datetime) -> None:
        knowledge_base.catalog_revision += 1
        knowledge_base.updated_at = now

    @staticmethod
    def _append_legacy_tombstone(
        db: Session,
        *,
        document: Document,
        identity: str,
        collection: str,
        content_sha256: str,
        source_object_key: str,
        now: datetime,
        cleanup_after: datetime | None = None,
    ) -> DocumentVersion:
        document.version_counter += 1
        tombstone = DocumentVersion(
            id=_new_id("docver"),
            document_id=document.id,
            version_number=document.version_counter,
            content_sha256=content_sha256,
            build_fingerprint=_payload_hash(
                {
                    "schema_version": 1,
                    "storage_layout": StorageLayout.LEGACY_FILENAME,
                    "legacy_identity": identity,
                    "state": "tombstone",
                }
            ),
            source_object_key=source_object_key,
            media_type="",
            size_bytes=0,
            parser_version=_LEGACY_TOMBSTONE_PARSER,
            chunker_version=_LEGACY_TOMBSTONE_PARSER,
            embedding_model="legacy",
            index_version="legacy-tombstone-v1",
            storage_layout=StorageLayout.LEGACY_FILENAME,
            vector_collection=collection,
            legacy_identity=identity,
            status=DocumentVersionStatus.SUPERSEDED,
            chunk_count=0,
            parent_chunk_count=0,
            superseded_at=now,
            cleanup_after=cleanup_after,
        )
        db.add(tombstone)
        db.flush()
        return tombstone

    @staticmethod
    def _knowledge_base(
        db: Session,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        lock: bool = False,
    ) -> KnowledgeBase:
        query = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.status == "active",
        )
        if lock:
            query = query.with_for_update()
        row = query.first()
        if not row:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "知识库不存在",
                status_code=404,
            )
        return row

    @staticmethod
    def _document_query(
        db: Session,
        *,
        tenant_id: str,
        canonical_name: str,
        knowledge_base_id: str | None,
    ):
        query = db.query(Document).filter(
            Document.tenant_id == tenant_id,
            Document.canonical_name == canonical_name,
        )
        if knowledge_base_id is not None:
            query = query.filter(Document.knowledge_base_id == knowledge_base_id)
        return query

    @classmethod
    def _document(
        cls,
        db: Session,
        *,
        tenant_id: str,
        canonical_name: str,
        knowledge_base_id: str | None,
        lock: bool = False,
    ) -> Document | None:
        query = cls._document_query(
            db,
            tenant_id=tenant_id,
            canonical_name=canonical_name,
            knowledge_base_id=knowledge_base_id,
        ).order_by(Document.id.asc())
        if lock:
            query = query.with_for_update()
        rows = query.limit(2).all()
        if len(rows) > 1:
            raise AppError(
                ErrorCode.CONFLICT,
                "canonical_name 在多个知识库中不唯一，请指定 knowledge_base_id",
                status_code=409,
            )
        return rows[0] if rows else None

    @staticmethod
    def _version_by_pointer(
        db: Session,
        document: Document,
        version_id: str | None,
    ) -> DocumentVersion | None:
        if not version_id:
            return None
        version = (
            db.query(DocumentVersion).filter(DocumentVersion.id == version_id).first()
        )
        if not version or version.document_id != document.id:
            raise AppError(
                ErrorCode.CONFLICT,
                "文档版本指针不属于当前文档",
                status_code=409,
            )
        return version

    @staticmethod
    def _job_for_version(db: Session, version_id: str) -> IndexJob | None:
        return (
            db.query(IndexJob)
            .filter(IndexJob.document_version_id == version_id)
            .first()
        )

    def _document_records(
        self,
        db: Session,
        documents: Iterable[Document],
    ) -> list[DocumentRecord]:
        rows = list(documents)
        if not rows:
            return []
        version_ids = {
            pointer
            for row in rows
            for pointer in (row.current_version_id, row.pending_version_id)
            if pointer
        }
        versions = (
            db.query(DocumentVersion).filter(DocumentVersion.id.in_(version_ids)).all()
            if version_ids
            else []
        )
        version_map = {row.id: row for row in versions}
        pending_ids = {row.pending_version_id for row in rows if row.pending_version_id}
        jobs = (
            db.query(IndexJob)
            .filter(IndexJob.document_version_id.in_(pending_ids))
            .all()
            if pending_ids
            else []
        )
        job_map = {row.document_version_id: row for row in jobs}
        knowledge_base_ids = {row.knowledge_base_id for row in rows}
        revisions = {
            row.id: row.catalog_revision
            for row in db.query(KnowledgeBase)
            .filter(KnowledgeBase.id.in_(knowledge_base_ids))
            .all()
        }
        records: list[DocumentRecord] = []
        for document in rows:
            current = version_map.get(document.current_version_id)
            pending = version_map.get(document.pending_version_id)
            for pointer in (current, pending):
                if pointer and pointer.document_id != document.id:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "文档版本指针不属于当前文档",
                        status_code=409,
                    )
            pending_job = job_map.get(document.pending_version_id)
            records.append(
                DocumentRecord(
                    id=document.id,
                    tenant_id=document.tenant_id,
                    knowledge_base_id=document.knowledge_base_id,
                    canonical_name=document.canonical_name,
                    owner_id=document.owner_id,
                    status=document.status,
                    publication_fence=document.publication_fence,
                    version_counter=document.version_counter,
                    catalog_revision=revisions.get(document.knowledge_base_id, 0),
                    current_version=(
                        self._version_record(current) if current else None
                    ),
                    pending_version=(
                        self._version_record(pending) if pending else None
                    ),
                    pending_job=(
                        self._job_record(pending_job, document=document)
                        if pending_job
                        else None
                    ),
                    deleted_at=document.deleted_at,
                    created_at=document.created_at,
                    updated_at=document.updated_at,
                )
            )
        return records

    @staticmethod
    def _job_graph(
        db: Session,
        *,
        job_id: str,
        tenant_id: str | None,
        lock: bool,
    ) -> tuple[IndexJob, DocumentVersion, Document, KnowledgeBase]:
        query = (
            db.query(IndexJob, DocumentVersion, Document, KnowledgeBase)
            .join(
                DocumentVersion,
                DocumentVersion.id == IndexJob.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .filter(IndexJob.id == job_id)
        )
        if tenant_id is not None:
            query = query.filter(Document.tenant_id == tenant_id)
        if lock:
            query = query.with_for_update()
        row = query.first()
        if not row:
            raise AppError(ErrorCode.NOT_FOUND, "索引任务不存在", status_code=404)
        return row

    def ensure_knowledge_base(
        self,
        *,
        tenant_id: str,
        owner_id: int,
        name: str,
        knowledge_base_id: str | None = None,
    ) -> KnowledgeBaseRecord:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        normalized_name = _required_text(name, "name", 160)
        for attempt in range(2):
            db = self._session_factory()
            try:
                with db.begin():
                    row = (
                        db.query(KnowledgeBase)
                        .filter(
                            KnowledgeBase.tenant_id == tenant,
                            KnowledgeBase.name == normalized_name,
                        )
                        .with_for_update()
                        .first()
                    )
                    if row:
                        if row.owner_id != owner_id:
                            raise AppError(
                                ErrorCode.PERMISSION_DENIED,
                                "无权使用该知识库",
                                status_code=403,
                            )
                        return self._knowledge_base_record(row)
                    row = KnowledgeBase(
                        id=knowledge_base_id or _new_id("kb"),
                        tenant_id=tenant,
                        name=normalized_name,
                        owner_id=owner_id,
                        status="active",
                        catalog_revision=0,
                    )
                    db.add(row)
                    db.flush()
                    return self._knowledge_base_record(row)
            except IntegrityError as exc:
                db.rollback()
                if attempt == 0:
                    continue
                raise AppError(
                    ErrorCode.CONFLICT,
                    "知识库并发创建冲突，请重试",
                    status_code=409,
                    retryable=True,
                ) from exc
            finally:
                db.close()
        raise AssertionError("unreachable")

    def find_knowledge_base(
        self,
        *,
        tenant_id: str,
        name: str,
    ) -> KnowledgeBaseRecord | None:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        normalized_name = _required_text(name, "name", 160)
        db = self._session_factory()
        try:
            row = (
                db.query(KnowledgeBase)
                .filter(
                    KnowledgeBase.tenant_id == tenant,
                    KnowledgeBase.name == normalized_name,
                    KnowledgeBase.status == "active",
                )
                .first()
            )
            return self._knowledge_base_record(row) if row else None
        finally:
            db.close()

    def mark_legacy_adoption_complete(
        self,
        *,
        tenant_id: str,
        legacy_collection: str,
        knowledge_base_name: str,
        corpus_fingerprint: str,
        adoption_fence: int,
        knowledge_base_id: str | None = None,
    ) -> LegacyAdoptionState:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        collection = _required_text(legacy_collection, "legacy_collection", 160)
        target_name = _required_text(knowledge_base_name, "knowledge_base_name", 160)
        target_id = (
            _required_text(knowledge_base_id, "knowledge_base_id", 64)
            if knowledge_base_id is not None
            else None
        )
        fingerprint = _content_hash(corpus_fingerprint)
        if adoption_fence <= 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "adoption_fence 必须为正整数",
                status_code=400,
            )
        db = self._session_factory()
        try:
            with db.begin():
                if target_id is not None:
                    target_knowledge_base = self._knowledge_base(
                        db,
                        tenant_id=tenant,
                        knowledge_base_id=target_id,
                        lock=True,
                    )
                    if target_knowledge_base.name != target_name:
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "legacy 接管目标知识库不匹配",
                            status_code=409,
                        )
                row = (
                    db.query(DocumentCatalogState)
                    .filter(DocumentCatalogState.tenant_id == tenant)
                    .with_for_update()
                    .first()
                )
                now = utcnow()
                if (
                    row is None
                    or row.legacy_collection != collection
                    or row.legacy_knowledge_base_name != target_name
                    or row.legacy_corpus_fingerprint != fingerprint
                    or row.legacy_adoption_fence != adoption_fence
                    or (
                        row.legacy_knowledge_base_id is not None
                        and row.legacy_knowledge_base_id != target_id
                    )
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "legacy 接管 fencing 已失效，请重新扫描",
                        status_code=409,
                        retryable=True,
                    )
                row.legacy_collection = collection
                row.legacy_knowledge_base_id = target_id
                row.legacy_knowledge_base_name = target_name
                row.legacy_adoption_completed_at = now
                row.legacy_corpus_fingerprint = fingerprint
                row.updated_at = now
                db.flush()
                return LegacyAdoptionState(
                    tenant_id=tenant,
                    legacy_collection=collection,
                    knowledge_base_id=target_id,
                    knowledge_base_name=target_name,
                    state_exists=True,
                    complete=True,
                    fingerprint=fingerprint,
                    fence=adoption_fence,
                )
        finally:
            db.close()

    def begin_legacy_adoption(
        self,
        *,
        tenant_id: str,
        legacy_collection: str,
        knowledge_base_name: str,
        corpus_fingerprint: str,
    ) -> LegacyAdoptionState:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        collection = _required_text(legacy_collection, "legacy_collection", 160)
        target_name = _required_text(knowledge_base_name, "knowledge_base_name", 160)
        fingerprint = _content_hash(corpus_fingerprint)
        db = self._session_factory()
        try:
            with db.begin():
                row = (
                    db.query(DocumentCatalogState)
                    .filter(DocumentCatalogState.tenant_id == tenant)
                    .with_for_update()
                    .first()
                )
                now = utcnow()
                if row is None:
                    row = DocumentCatalogState(
                        tenant_id=tenant,
                        legacy_collection=collection,
                        legacy_knowledge_base_name=target_name,
                        legacy_adoption_fence=1,
                        legacy_corpus_fingerprint=fingerprint,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                    db.flush()
                    return LegacyAdoptionState(
                        tenant_id=tenant,
                        legacy_collection=collection,
                        knowledge_base_id=None,
                        knowledge_base_name=target_name,
                        state_exists=True,
                        complete=False,
                        fingerprint=fingerprint,
                        fence=1,
                    )
                same_projection = bool(
                    row.legacy_collection == collection
                    and row.legacy_knowledge_base_name == target_name
                    and row.legacy_corpus_fingerprint == fingerprint
                )
                if same_projection:
                    return LegacyAdoptionState(
                        tenant_id=tenant,
                        legacy_collection=collection,
                        knowledge_base_id=row.legacy_knowledge_base_id,
                        knowledge_base_name=target_name,
                        state_exists=True,
                        complete=row.legacy_adoption_completed_at is not None,
                        fingerprint=fingerprint,
                        fence=row.legacy_adoption_fence,
                    )
                row.legacy_collection = collection
                row.legacy_knowledge_base_id = None
                row.legacy_knowledge_base_name = target_name
                row.legacy_adoption_fence += 1
                row.legacy_adoption_completed_at = None
                row.legacy_corpus_fingerprint = fingerprint
                row.updated_at = now
                db.flush()
                return LegacyAdoptionState(
                    tenant_id=tenant,
                    legacy_collection=collection,
                    knowledge_base_id=None,
                    knowledge_base_name=target_name,
                    state_exists=True,
                    complete=False,
                    fingerprint=fingerprint,
                    fence=row.legacy_adoption_fence,
                )
        finally:
            db.close()

    def bootstrap_empty_legacy_corpus(
        self,
        *,
        tenant_id: str,
        legacy_collection: str,
        knowledge_base_name: str,
    ) -> LegacyAdoptionState:
        collection = _required_text(legacy_collection, "legacy_collection", 160)
        fingerprint = _payload_hash(
            {
                "schema_version": 1,
                "collection_name": collection,
                "legacy_collection_missing": True,
                "documents": [],
            }
        )
        reservation = self.begin_legacy_adoption(
            tenant_id=tenant_id,
            legacy_collection=collection,
            knowledge_base_name=knowledge_base_name,
            corpus_fingerprint=fingerprint,
        )
        return self.mark_legacy_adoption_complete(
            tenant_id=tenant_id,
            legacy_collection=collection,
            knowledge_base_name=knowledge_base_name,
            corpus_fingerprint=fingerprint,
            adoption_fence=reservation.fence,
        )

    def legacy_adoption_state(
        self,
        *,
        tenant_id: str,
    ) -> LegacyAdoptionState:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        db = self._session_factory()
        try:
            row = (
                db.query(DocumentCatalogState)
                .filter(DocumentCatalogState.tenant_id == tenant)
                .first()
            )
            if row is None:
                return LegacyAdoptionState(
                    tenant_id=tenant,
                    legacy_collection=None,
                    knowledge_base_id=None,
                    knowledge_base_name=None,
                    state_exists=False,
                    complete=False,
                    fingerprint=_payload_hash(
                        {
                            "schema_version": 1,
                            "tenant_id": tenant,
                            "legacy_adoption_state": "missing",
                        }
                    ),
                    fence=0,
                )
            complete = bool(
                row.legacy_adoption_completed_at is not None
                and isinstance(row.legacy_corpus_fingerprint, str)
                and _SHA256_RE.fullmatch(row.legacy_corpus_fingerprint)
            )
            return LegacyAdoptionState(
                tenant_id=tenant,
                legacy_collection=row.legacy_collection,
                knowledge_base_id=row.legacy_knowledge_base_id,
                knowledge_base_name=row.legacy_knowledge_base_name,
                state_exists=True,
                complete=complete,
                fingerprint=(
                    row.legacy_corpus_fingerprint
                    if complete
                    else _payload_hash(
                        {
                            "schema_version": 1,
                            "tenant_id": tenant,
                            "legacy_collection": row.legacy_collection,
                            "legacy_knowledge_base_id": (row.legacy_knowledge_base_id),
                            "legacy_knowledge_base_name": (
                                row.legacy_knowledge_base_name
                            ),
                            "legacy_adoption_fence": row.legacy_adoption_fence,
                            "legacy_corpus_fingerprint": (
                                row.legacy_corpus_fingerprint
                            ),
                            "legacy_adoption_state": "incomplete",
                        }
                    )
                ),
                fence=row.legacy_adoption_fence,
            )
        finally:
            db.close()

    def reserve_upload(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        canonical_name: str,
        owner_id: int,
        content_sha256: str,
        source_object_key: str,
        media_type: str,
        size_bytes: int,
        processing_profile: BuildProfile,
        vector_collection: str = "",
        max_attempts: int = 3,
        cleanup_grace: timedelta = _DEFAULT_CLEANUP_GRACE,
    ) -> UploadReservation:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        knowledge_base_key = _required_text(knowledge_base_id, "knowledge_base_id", 64)
        name = _required_text(canonical_name, "canonical_name", 255)
        digest = _content_hash(content_sha256)
        source_key = _required_text(source_object_key, "source_object_key", 512)
        media = str(media_type or "").strip()[:160]
        if size_bytes < 0 or max_attempts < 1:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "size_bytes 和 max_attempts 必须为有效正数",
                status_code=400,
            )
        profile = BuildProfile(
            parser_version=_required_text(
                processing_profile.parser_version, "parser_version", 64
            ),
            chunker_version=_required_text(
                processing_profile.chunker_version, "chunker_version", 64
            ),
            embedding_model=str(processing_profile.embedding_model or "").strip()[:160],
            index_version=_required_text(
                processing_profile.index_version, "index_version", 64
            ),
        )
        collection = str(vector_collection or "").strip()[:160]
        for attempt in range(2):
            db = self._session_factory()
            try:
                with db.begin():
                    knowledge_base = self._knowledge_base(
                        db,
                        tenant_id=tenant,
                        knowledge_base_id=knowledge_base_key,
                        lock=True,
                    )
                    document = self._document(
                        db,
                        tenant_id=tenant,
                        canonical_name=name,
                        knowledge_base_id=knowledge_base_key,
                        lock=True,
                    )
                    now = utcnow()
                    if document and document.owner_id != owner_id:
                        raise AppError(
                            ErrorCode.PERMISSION_DENIED,
                            "无权更新该文档",
                            status_code=403,
                        )
                    if document is None:
                        document = Document(
                            id=_new_id("doc"),
                            tenant_id=tenant,
                            knowledge_base_id=knowledge_base_key,
                            canonical_name=name,
                            owner_id=owner_id,
                            status="pending",
                            publication_fence=0,
                            version_counter=0,
                        )
                        db.add(document)
                        db.flush()
                    matching_versions = (
                        db.query(DocumentVersion)
                        .filter(
                            DocumentVersion.document_id == document.id,
                            DocumentVersion.content_sha256 == digest,
                            DocumentVersion.build_fingerprint == profile.fingerprint,
                        )
                        .order_by(DocumentVersion.version_number.desc())
                        .all()
                    )
                    matching_by_id = {row.id: row for row in matching_versions}
                    current_match = matching_by_id.get(document.current_version_id)
                    if current_match is not None:
                        if current_match.status != DocumentVersionStatus.READY:
                            raise AppError(
                                ErrorCode.CONFLICT,
                                "current_version 身份匹配但未处于 ready 状态",
                                status_code=409,
                            )
                        job = self._job_for_version(db, current_match.id)
                        if job is None:
                            job = IndexJob(
                                id=_new_id("idxjob"),
                                document_version_id=current_match.id,
                                status=IndexJobStatus.COMPLETED,
                                current_step="published",
                                progress=100,
                                max_attempts=max_attempts,
                                publication_fence=document.publication_fence,
                                expected_current_version_id=current_match.id,
                                finished_at=current_match.published_at or now,
                                step_state_json={"adopted_existing": True},
                            )
                            db.add(job)
                            db.flush()
                        elif job.status != IndexJobStatus.COMPLETED:
                            raise AppError(
                                ErrorCode.CONFLICT,
                                "current_version 对应索引任务未完成",
                                status_code=409,
                            )
                        record = self._document_records(db, [document])[0]
                        return UploadReservation(
                            document=record,
                            version=self._version_record(current_match),
                            job=self._job_record(job, document=document),
                            created=False,
                            requeued=False,
                            already_current=True,
                            publication_fence=document.publication_fence,
                            expected_current_version_id=current_match.id,
                        )
                    pending_match = matching_by_id.get(document.pending_version_id)
                    pending_job = (
                        self._job_for_version(db, pending_match.id)
                        if pending_match is not None
                        else None
                    )
                    if (
                        pending_match is not None
                        and pending_match.status in _ACTIVE_VERSION_STATUSES
                        and pending_job is not None
                        and pending_job.publication_fence == document.publication_fence
                        and pending_job.status
                        not in {
                            IndexJobStatus.FAILED,
                            IndexJobStatus.CANCELLED,
                            IndexJobStatus.DEAD_LETTER,
                        }
                    ):
                        record = self._document_records(db, [document])[0]
                        return UploadReservation(
                            document=record,
                            version=self._version_record(pending_match),
                            job=self._job_record(pending_job, document=document),
                            created=False,
                            requeued=False,
                            already_current=False,
                            publication_fence=document.publication_fence,
                            expected_current_version_id=pending_job.expected_current_version_id,
                        )

                    pointer_ids = {
                        value
                        for value in (
                            document.current_version_id,
                            document.pending_version_id,
                        )
                        if value is not None
                    }
                    orphaned_active = [
                        row
                        for row in matching_versions
                        if row.status in _ACTIVE_VERSION_STATUSES
                        and row.id not in pointer_ids
                    ]
                    if orphaned_active:
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "存在未被 current/pending 指针持有的活跃同身份版本",
                            status_code=409,
                        )

                    cleanup_after = _cleanup_at(now, cleanup_grace)
                    old_pending = self._version_by_pointer(
                        db, document, document.pending_version_id
                    )
                    if old_pending:
                        self._supersede_version(
                            db,
                            old_pending,
                            now=now,
                            cleanup_after=cleanup_after,
                            cancel_job=True,
                        )
                        # The active-identity partial unique index must observe the
                        # terminal transition before a same-identity replacement is
                        # inserted. A terminal version ID is immutable thereafter so
                        # a stale cleanup snapshot can never delete a later build.
                        db.flush()

                    requeued = bool(matching_versions)
                    document.version_counter += 1
                    candidate = DocumentVersion(
                        id=_new_id("docver"),
                        document_id=document.id,
                        version_number=document.version_counter,
                        content_sha256=digest,
                        build_fingerprint=profile.fingerprint,
                        source_object_key=source_key,
                        media_type=media,
                        size_bytes=size_bytes,
                        parser_version=profile.parser_version,
                        chunker_version=profile.chunker_version,
                        embedding_model=profile.embedding_model,
                        index_version=profile.index_version,
                        storage_layout=StorageLayout.VERSIONED,
                        vector_collection=collection,
                        status=DocumentVersionStatus.UPLOADED,
                        chunk_count=0,
                        parent_chunk_count=0,
                    )
                    db.add(candidate)
                    db.flush()

                    document.publication_fence += 1
                    document.pending_version_id = candidate.id
                    document.status = (
                        "updating" if document.current_version_id else "indexing"
                    )
                    document.deleted_at = None
                    document.updated_at = now
                    job = IndexJob(
                        id=_new_id("idxjob"),
                        document_version_id=candidate.id,
                        max_attempts=max_attempts,
                    )
                    db.add(job)
                    job.status = IndexJobStatus.PENDING
                    job.current_step = "uploaded"
                    job.progress = 0
                    job.publication_fence = document.publication_fence
                    job.expected_current_version_id = document.current_version_id
                    job.owner_worker_id = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                    job.next_retry_at = None
                    job.error_code = None
                    job.error_detail_redacted = None
                    job.finished_at = None
                    job.step_state_json = {
                        "build_fingerprint": profile.fingerprint,
                        "storage_layout": StorageLayout.VERSIONED,
                    }
                    job.updated_at = now
                    self._bump_revision(knowledge_base, now)
                    db.flush()
                    record = self._document_records(db, [document])[0]
                    return UploadReservation(
                        document=record,
                        version=self._version_record(candidate),
                        job=self._job_record(job, document=document),
                        created=True,
                        requeued=requeued,
                        already_current=False,
                        publication_fence=document.publication_fence,
                        expected_current_version_id=document.current_version_id,
                    )
            except IntegrityError as exc:
                db.rollback()
                if attempt == 0:
                    continue
                raise AppError(
                    ErrorCode.CONFLICT,
                    "文档候选版本并发预留冲突，请重试",
                    status_code=409,
                    retryable=True,
                ) from exc
            finally:
                db.close()
        raise AssertionError("unreachable")

    @staticmethod
    def _supersede_version(
        db: Session,
        version: DocumentVersion,
        *,
        now: datetime,
        cleanup_after: datetime,
        cancel_job: bool,
    ) -> None:
        version.status = DocumentVersionStatus.SUPERSEDED
        version.superseded_at = now
        version.cleanup_after = cleanup_after
        version.updated_at = now
        if cancel_job:
            job = DocumentCatalog._job_for_version(db, version.id)
            if job and job.status not in {
                IndexJobStatus.COMPLETED,
                IndexJobStatus.FAILED,
                IndexJobStatus.CANCELLED,
                IndexJobStatus.DEAD_LETTER,
            }:
                job.status = IndexJobStatus.CANCELLED
                job.current_step = "superseded"
                job.finished_at = now
                job.owner_worker_id = None
                job.lease_expires_at = None
                job.updated_at = now

    @staticmethod
    def _manifest_entry(value: ManifestEntry | Mapping) -> ManifestEntry:
        entry = (
            value if isinstance(value, ManifestEntry) else ManifestEntry(**dict(value))
        )
        chunk_id = _required_text(entry.chunk_id, "chunk_id", 512)
        content_hash = _content_hash(entry.content_hash)
        store_kind = _required_text(entry.store_kind, "store_kind", 32)
        if store_kind not in {"vector", "parent"}:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "store_kind 仅支持 vector 或 parent",
                status_code=400,
            )
        section_id = str(entry.section_id or "").strip()[:256]
        if entry.chunk_level < 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "chunk_level 不能为负数",
                status_code=400,
            )
        return ManifestEntry(
            chunk_id=chunk_id,
            content_hash=content_hash,
            store_kind=store_kind,
            section_id=section_id,
            chunk_level=int(entry.chunk_level),
        )

    def record_manifest(
        self,
        *,
        job_id: str,
        publication_fence: int,
        entries: Iterable[ManifestEntry | Mapping],
        vector_chunk_count: int | None = None,
        parent_chunk_count: int | None = None,
    ) -> VersionBuild:
        normalized: dict[tuple[str, str], ManifestEntry] = {}
        for value in entries:
            entry = self._manifest_entry(value)
            key = (entry.store_kind, entry.chunk_id)
            if key in normalized:
                raise AppError(
                    ErrorCode.INVALID_REQUEST,
                    "manifest 中存在重复的 (store_kind, chunk_id)",
                    status_code=400,
                )
            normalized[key] = entry
        inferred_vectors = sum(
            1 for entry in normalized.values() if entry.store_kind == "vector"
        )
        inferred_parents = sum(
            1 for entry in normalized.values() if entry.store_kind == "parent"
        )
        vector_count = (
            inferred_vectors if vector_chunk_count is None else vector_chunk_count
        )
        parent_count = (
            inferred_parents if parent_chunk_count is None else parent_chunk_count
        )
        if vector_count < 1 or parent_count < 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "已验证的向量 chunk 数必须大于 0",
                status_code=400,
            )
        if vector_count != inferred_vectors or parent_count != inferred_parents:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "显式 chunk count 必须与 exact manifest 完全一致",
                status_code=400,
            )
        db = self._session_factory()
        try:
            with db.begin():
                job, version, document, knowledge_base = self._job_graph(
                    db, job_id=job_id, tenant_id=None, lock=True
                )
                self._assert_candidate(
                    document,
                    job,
                    version,
                    publication_fence=publication_fence,
                )
                now = utcnow()
                db.query(IndexManifest).filter(
                    IndexManifest.document_version_id == version.id
                ).delete(synchronize_session=False)
                db.add_all(
                    [
                        IndexManifest(
                            document_version_id=version.id,
                            chunk_id=entry.chunk_id,
                            store_kind=entry.store_kind,
                            section_id=entry.section_id,
                            chunk_level=entry.chunk_level,
                            content_hash=entry.content_hash,
                            indexed_at=now,
                        )
                        for entry in normalized.values()
                    ]
                )
                version.status = DocumentVersionStatus.STAGED
                version.chunk_count = int(vector_count)
                version.parent_chunk_count = int(parent_count)
                version.error_code = None
                version.error_detail_redacted = None
                version.updated_at = now
                job.status = IndexJobStatus.STAGED
                job.current_step = "verified"
                job.progress = 95
                job.step_state_json = {
                    **(job.step_state_json or {}),
                    "manifest_entry_count": len(normalized),
                    "vector_chunk_count": vector_count,
                    "parent_chunk_count": parent_count,
                }
                job.updated_at = now
                self._bump_revision(knowledge_base, now)
                db.flush()
                return VersionBuild(
                    job=self._job_record(job, document=document),
                    document=self._document_records(db, [document])[0],
                    version=self._version_record(version),
                )
        finally:
            db.close()

    @staticmethod
    def _assert_candidate(
        document: Document,
        job: IndexJob,
        version: DocumentVersion,
        *,
        publication_fence: int,
    ) -> None:
        if job.status in {
            IndexJobStatus.COMPLETED,
            IndexJobStatus.FAILED,
            IndexJobStatus.CANCELLED,
            IndexJobStatus.DEAD_LETTER,
        }:
            raise AppError(
                ErrorCode.CONFLICT,
                f"终态索引任务 {job.status} 不能继续写入候选版本",
                status_code=409,
            )
        if (
            publication_fence != job.publication_fence
            or publication_fence != document.publication_fence
            or document.pending_version_id != version.id
        ):
            raise AppError(
                ErrorCode.CONFLICT,
                "候选版本 fencing token 已失效",
                status_code=409,
                safe_details={"current_publication_fence": document.publication_fence},
            )
        if document.deleted_at is not None:
            raise AppError(
                ErrorCode.CONFLICT,
                "已删除文档不能继续发布",
                status_code=409,
            )

    def publish(
        self,
        *,
        job_id: str,
        publication_fence: int,
        expected_current_version_id: str | None,
        cleanup_grace: timedelta = _DEFAULT_CLEANUP_GRACE,
    ) -> PublicationResult:
        db = self._session_factory()
        try:
            with db.begin():
                job, version, document, knowledge_base = self._job_graph(
                    db, job_id=job_id, tenant_id=None, lock=True
                )
                if (
                    document.current_version_id == version.id
                    and version.status == DocumentVersionStatus.READY
                    and job.status == IndexJobStatus.COMPLETED
                ):
                    return PublicationResult(
                        document=self._document_records(db, [document])[0],
                        version=self._version_record(version),
                        previous_version=None,
                        published=False,
                    )
                self._assert_candidate(
                    document,
                    job,
                    version,
                    publication_fence=publication_fence,
                )
                if job.expected_current_version_id != expected_current_version_id:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "预期 current_version 与任务预留不一致",
                        status_code=409,
                    )
                if version.status != DocumentVersionStatus.STAGED:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "候选版本尚未完成索引验证",
                        status_code=409,
                    )
                vector_manifest_count = (
                    db.query(IndexManifest)
                    .filter(
                        IndexManifest.document_version_id == version.id,
                        IndexManifest.store_kind == "vector",
                    )
                    .count()
                )
                parent_manifest_count = (
                    db.query(IndexManifest)
                    .filter(
                        IndexManifest.document_version_id == version.id,
                        IndexManifest.store_kind == "parent",
                    )
                    .count()
                )
                if (
                    vector_manifest_count < 1
                    or vector_manifest_count != version.chunk_count
                    or parent_manifest_count != version.parent_chunk_count
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "候选版本 exact manifest 计数不一致",
                        status_code=409,
                    )
                current_predicate = (
                    Document.current_version_id.is_(None)
                    if expected_current_version_id is None
                    else Document.current_version_id == expected_current_version_id
                )
                now = utcnow()
                result = db.execute(
                    update(Document)
                    .where(
                        Document.id == document.id,
                        Document.deleted_at.is_(None),
                        Document.pending_version_id == version.id,
                        Document.publication_fence == publication_fence,
                        current_predicate,
                    )
                    .values(
                        current_version_id=version.id,
                        pending_version_id=None,
                        status="ready",
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "文档 current_version CAS 冲突",
                        status_code=409,
                        retryable=True,
                    )
                previous = self._version_by_pointer(
                    db, document, expected_current_version_id
                )
                if previous and previous.id != version.id:
                    self._supersede_version(
                        db,
                        previous,
                        now=now,
                        cleanup_after=_cleanup_at(now, cleanup_grace),
                        cancel_job=False,
                    )
                version.status = DocumentVersionStatus.READY
                version.published_at = now
                version.superseded_at = None
                version.cleanup_after = None
                version.index_cleaned_at = None
                version.cleanup_error_code = None
                version.updated_at = now
                job.status = IndexJobStatus.COMPLETED
                job.current_step = "published"
                job.progress = 100
                job.owner_worker_id = None
                job.lease_expires_at = None
                job.finished_at = now
                job.updated_at = now
                self._bump_revision(knowledge_base, now)
                db.flush()
                db.expire(document)
                return PublicationResult(
                    document=self._document_records(db, [document])[0],
                    version=self._version_record(version),
                    previous_version=(
                        self._version_record(previous) if previous else None
                    ),
                    published=True,
                )
        finally:
            db.close()

    def _fail_locked(
        self,
        db: Session,
        *,
        job: IndexJob,
        version: DocumentVersion,
        document: Document,
        knowledge_base: KnowledgeBase,
        publication_fence: int,
        error_code: str,
        error_detail_redacted: str | None,
        step_state_patch: Mapping | None = None,
    ) -> IndexJobRecord:
        if publication_fence != job.publication_fence:
            raise AppError(
                ErrorCode.CONFLICT,
                "索引任务 fencing token 已失效",
                status_code=409,
            )
        if job.status == IndexJobStatus.FAILED:
            return self._job_record(job, document=document)
        if job.status in {
            IndexJobStatus.COMPLETED,
            IndexJobStatus.CANCELLED,
            IndexJobStatus.DEAD_LETTER,
        }:
            raise AppError(
                ErrorCode.CONFLICT,
                f"终态任务 {job.status} 不能改写为 failed",
                status_code=409,
            )
        now = utcnow()
        job.status = IndexJobStatus.FAILED
        job.current_step = "failed"
        job.error_code = error_code
        job.error_detail_redacted = error_detail_redacted
        job.step_state_json = {
            **(job.step_state_json or {}),
            **dict(step_state_patch or {}),
        }
        job.owner_worker_id = None
        job.lease_expires_at = None
        job.finished_at = now
        job.updated_at = now
        active_candidate = (
            document.pending_version_id == version.id
            and document.publication_fence == publication_fence
        )
        if active_candidate:
            version.status = DocumentVersionStatus.FAILED
            version.error_code = error_code
            version.error_detail_redacted = error_detail_redacted
            version.cleanup_after = now
            version.index_cleaned_at = None
            version.cleanup_error_code = None
            version.updated_at = now
            result = db.execute(
                update(Document)
                .where(
                    Document.id == document.id,
                    Document.pending_version_id == version.id,
                    Document.publication_fence == publication_fence,
                )
                .values(
                    pending_version_id=None,
                    status=("ready" if document.current_version_id else "failed"),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise AppError(
                    ErrorCode.CONFLICT,
                    "失败状态 CAS 冲突",
                    status_code=409,
                    retryable=True,
                )
            db.expire(document)
        elif version.status != DocumentVersionStatus.SUPERSEDED:
            version.status = DocumentVersionStatus.FAILED
            version.error_code = error_code
            version.error_detail_redacted = error_detail_redacted
            version.cleanup_after = now
            version.index_cleaned_at = None
            version.cleanup_error_code = None
            version.updated_at = now
        self._bump_revision(knowledge_base, now)
        db.flush()
        return self._job_record(job, document=document)

    def fail(
        self,
        *,
        job_id: str,
        publication_fence: int,
        error_code: str,
        error_detail_redacted: str | None = None,
    ) -> IndexJobRecord:
        code = _required_text(error_code, "error_code", 64)
        detail = str(error_detail_redacted or "")[:2000] or None
        db = self._session_factory()
        try:
            with db.begin():
                job, version, document, knowledge_base = self._job_graph(
                    db, job_id=job_id, tenant_id=None, lock=True
                )
                return self._fail_locked(
                    db,
                    job=job,
                    version=version,
                    document=document,
                    knowledge_base=knowledge_base,
                    publication_fence=publication_fence,
                    error_code=code,
                    error_detail_redacted=detail,
                )
        finally:
            db.close()

    def update_job(
        self,
        *,
        job_id: str,
        publication_fence: int,
        status: str | IndexJobStatus | None = None,
        current_step: str | None = None,
        progress: int | None = None,
        step_state_patch: Mapping | None = None,
        increment_attempts: bool = False,
    ) -> IndexJobRecord:
        target_status = str(status) if status is not None else None
        if target_status is not None and target_status not in _JOB_TRANSITIONS:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "未知索引任务状态",
                status_code=400,
            )
        if progress is not None and not 0 <= progress <= 100:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "progress 必须位于 0-100",
                status_code=400,
            )
        step = (
            _required_text(current_step, "current_step", 64)
            if current_step is not None
            else None
        )
        patch = dict(step_state_patch or {})
        db = self._session_factory()
        try:
            with db.begin():
                job, version, document, knowledge_base = self._job_graph(
                    db, job_id=job_id, tenant_id=None, lock=True
                )
                if publication_fence != job.publication_fence:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "索引任务 fencing token 已失效",
                        status_code=409,
                    )
                next_status = target_status or job.status
                if next_status not in _JOB_TRANSITIONS.get(job.status, set()):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        f"索引任务不能从 {job.status} 转换到 {next_status}",
                        status_code=409,
                    )
                if next_status == IndexJobStatus.FAILED:
                    code = _required_text(
                        str(patch.pop("error_code", "INDEX_BUILD_FAILED")),
                        "error_code",
                        64,
                    )
                    detail = str(patch.pop("error_detail_redacted", ""))[:2000] or None
                    return self._fail_locked(
                        db,
                        job=job,
                        version=version,
                        document=document,
                        knowledge_base=knowledge_base,
                        publication_fence=publication_fence,
                        error_code=code,
                        error_detail_redacted=detail,
                        step_state_patch=patch,
                    )
                if next_status == IndexJobStatus.COMPLETED and not (
                    document.current_version_id == version.id
                    and version.status == DocumentVersionStatus.READY
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "只有已原子发布的版本才能完成任务",
                        status_code=409,
                    )
                if next_status == IndexJobStatus.STAGED and (
                    version.status != DocumentVersionStatus.STAGED
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "必须先记录 exact manifest 才能进入 staged",
                        status_code=409,
                    )
                if next_status != IndexJobStatus.COMPLETED:
                    self._assert_candidate(
                        document,
                        job,
                        version,
                        publication_fence=publication_fence,
                    )
                if progress is not None and progress < job.progress:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "progress 不能倒退",
                        status_code=409,
                    )
                now = utcnow()
                job.status = next_status
                if step is not None:
                    job.current_step = step
                if progress is not None:
                    job.progress = progress
                if increment_attempts:
                    job.attempts += 1
                job.step_state_json = {**(job.step_state_json or {}), **patch}
                job.updated_at = now
                if next_status == IndexJobStatus.RUNNING:
                    version.status = (
                        DocumentVersionStatus.PARSING
                        if job.current_step in {"parse", "parsing"}
                        else DocumentVersionStatus.INDEXING
                    )
                    version.updated_at = now
                elif next_status in {
                    IndexJobStatus.CANCELLED,
                    IndexJobStatus.DEAD_LETTER,
                }:
                    if next_status == IndexJobStatus.CANCELLED:
                        self._supersede_version(
                            db,
                            version,
                            now=now,
                            cleanup_after=_cleanup_at(now, _DEFAULT_CLEANUP_GRACE),
                            cancel_job=False,
                        )
                    else:
                        version.status = DocumentVersionStatus.FAILED
                        version.error_code = _required_text(
                            str(patch.get("error_code", "INDEX_DEAD_LETTER")),
                            "error_code",
                            64,
                        )
                        version.error_detail_redacted = (
                            str(patch.get("error_detail_redacted", ""))[:2000] or None
                        )
                        version.cleanup_after = now
                        version.index_cleaned_at = None
                        version.cleanup_error_code = None
                        version.updated_at = now
                    result = db.execute(
                        update(Document)
                        .where(
                            Document.id == document.id,
                            Document.pending_version_id == version.id,
                            Document.publication_fence == publication_fence,
                        )
                        .values(
                            pending_version_id=None,
                            status=(
                                "ready" if document.current_version_id else "failed"
                            ),
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "终态任务清除 pending_version CAS 冲突",
                            status_code=409,
                            retryable=True,
                        )
                    job.finished_at = now
                    job.owner_worker_id = None
                    job.lease_expires_at = None
                    db.expire(document)
                self._bump_revision(knowledge_base, now)
                db.flush()
                return self._job_record(job, document=document)
        finally:
            db.close()

    def get_job(
        self,
        *,
        job_id: str,
        tenant_id: str | None = None,
    ) -> IndexJobRecord:
        db = self._session_factory()
        try:
            job, _version, document, _knowledge_base = self._job_graph(
                db,
                job_id=job_id,
                tenant_id=tenant_id,
                lock=False,
            )
            return self._job_record(job, document=document)
        finally:
            db.close()

    def list_jobs(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[IndexJobRecord]:
        if limit < 1 or limit > 1000 or offset < 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "limit 或 offset 无效",
                status_code=400,
            )
        db = self._session_factory()
        try:
            rows = (
                db.query(IndexJob, Document)
                .join(
                    DocumentVersion,
                    DocumentVersion.id == IndexJob.document_version_id,
                )
                .join(Document, Document.id == DocumentVersion.document_id)
                .filter(Document.tenant_id == tenant_id)
                .order_by(IndexJob.created_at.desc(), IndexJob.id.asc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [self._job_record(job, document=document) for job, document in rows]
        finally:
            db.close()

    def load_build(
        self,
        *,
        job_id: str,
        tenant_id: str | None = None,
    ) -> VersionBuild:
        db = self._session_factory()
        try:
            job, version, document, _knowledge_base = self._job_graph(
                db,
                job_id=job_id,
                tenant_id=tenant_id,
                lock=False,
            )
            return VersionBuild(
                job=self._job_record(job, document=document),
                document=self._document_records(db, [document])[0],
                version=self._version_record(version),
            )
        finally:
            db.close()

    def list_documents(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        if limit < 1 or limit > 1000 or offset < 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "limit 或 offset 无效",
                status_code=400,
            )
        db = self._session_factory()
        try:
            query = db.query(Document).filter(Document.tenant_id == tenant_id)
            if knowledge_base_id is not None:
                query = query.filter(Document.knowledge_base_id == knowledge_base_id)
            if not include_deleted:
                query = query.filter(Document.deleted_at.is_(None))
            rows = (
                query.order_by(Document.updated_at.desc(), Document.id.asc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return self._document_records(db, rows)
        finally:
            db.close()

    def get_current(
        self,
        *,
        tenant_id: str,
        canonical_name: str,
        knowledge_base_id: str | None = None,
    ) -> DocumentRecord | None:
        db = self._session_factory()
        try:
            document = self._document(
                db,
                tenant_id=tenant_id,
                canonical_name=canonical_name,
                knowledge_base_id=knowledge_base_id,
            )
            if (
                not document
                or document.deleted_at is not None
                or not document.current_version_id
            ):
                return None
            record = self._document_records(db, [document])[0]
            if (
                not record.current_version
                or record.current_version.status != DocumentVersionStatus.READY
            ):
                raise AppError(
                    ErrorCode.CONFLICT,
                    "current_version 尚未处于 ready 状态",
                    status_code=409,
                )
            return record
        finally:
            db.close()

    def retire(
        self,
        *,
        tenant_id: str,
        canonical_name: str,
        knowledge_base_id: str | None = None,
        cleanup_grace: timedelta = _DEFAULT_CLEANUP_GRACE,
    ) -> RetirementResult:
        name = _required_text(canonical_name, "canonical_name", 255)
        db = self._session_factory()
        try:
            with db.begin():
                document = self._document(
                    db,
                    tenant_id=tenant_id,
                    canonical_name=name,
                    knowledge_base_id=knowledge_base_id,
                    lock=True,
                )
                if not document:
                    return RetirementResult(
                        document_id=None,
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                        canonical_name=name,
                        found=False,
                        already_deleted=False,
                        cleanup_versions=(),
                    )
                current = self._version_by_pointer(
                    db, document, document.current_version_id
                )
                pending = self._version_by_pointer(
                    db, document, document.pending_version_id
                )
                if document.deleted_at is not None:
                    cleanup_rows = (
                        db.query(DocumentVersion)
                        .filter(
                            DocumentVersion.document_id == document.id,
                            DocumentVersion.status.in_(
                                {
                                    DocumentVersionStatus.FAILED,
                                    DocumentVersionStatus.SUPERSEDED,
                                }
                            ),
                            DocumentVersion.index_cleaned_at.is_(None),
                        )
                        .order_by(DocumentVersion.version_number.asc())
                        .all()
                    )
                    return RetirementResult(
                        document_id=document.id,
                        tenant_id=document.tenant_id,
                        knowledge_base_id=document.knowledge_base_id,
                        canonical_name=name,
                        found=True,
                        already_deleted=True,
                        cleanup_versions=tuple(
                            self._version_record(row) for row in cleanup_rows
                        ),
                    )
                knowledge_base = self._knowledge_base(
                    db,
                    tenant_id=document.tenant_id,
                    knowledge_base_id=document.knowledge_base_id,
                    lock=True,
                )
                now = utcnow()
                cleanup_after = _cleanup_at(now, cleanup_grace)
                if current:
                    self._supersede_version(
                        db,
                        current,
                        now=now,
                        cleanup_after=cleanup_after,
                        cancel_job=False,
                    )
                if pending and (not current or pending.id != current.id):
                    self._supersede_version(
                        db,
                        pending,
                        now=now,
                        cleanup_after=cleanup_after,
                        cancel_job=True,
                    )
                old_current_id = document.current_version_id
                old_pending_id = document.pending_version_id
                old_fence = document.publication_fence
                current_predicate = (
                    Document.current_version_id.is_(None)
                    if old_current_id is None
                    else Document.current_version_id == old_current_id
                )
                pending_predicate = (
                    Document.pending_version_id.is_(None)
                    if old_pending_id is None
                    else Document.pending_version_id == old_pending_id
                )
                result = db.execute(
                    update(Document)
                    .where(
                        Document.id == document.id,
                        Document.publication_fence == old_fence,
                        current_predicate,
                        pending_predicate,
                    )
                    .values(
                        current_version_id=None,
                        pending_version_id=None,
                        publication_fence=old_fence + 1,
                        status="deleted",
                        deleted_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "文档删除 CAS 冲突",
                        status_code=409,
                        retryable=True,
                    )
                self._bump_revision(knowledge_base, now)
                db.flush()
                return RetirementResult(
                    document_id=document.id,
                    tenant_id=document.tenant_id,
                    knowledge_base_id=document.knowledge_base_id,
                    canonical_name=name,
                    found=True,
                    already_deleted=False,
                    cleanup_versions=tuple(
                        self._version_record(row)
                        for row in {
                            row.id: row for row in (current, pending) if row
                        }.values()
                    ),
                )
        finally:
            db.close()

    def suppress_legacy_name(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        canonical_name: str,
        owner_id: int,
        vector_collection: str,
        cleanup_grace: timedelta = timedelta(0),
    ) -> RetirementResult:
        """Create a durable filename tombstone before legacy physical cleanup."""

        tenant = _required_text(tenant_id, "tenant_id", 64)
        knowledge_base_key = _required_text(knowledge_base_id, "knowledge_base_id", 64)
        name = _required_text(canonical_name, "canonical_name", 255)
        collection = _required_text(vector_collection, "vector_collection", 160)
        identity = legacy_source_identity(
            vector_collection=collection,
            canonical_name=name,
        )
        db = self._session_factory()
        try:
            with db.begin():
                knowledge_base = self._knowledge_base(
                    db,
                    tenant_id=tenant,
                    knowledge_base_id=knowledge_base_key,
                    lock=True,
                )
                document = self._document(
                    db,
                    tenant_id=tenant,
                    canonical_name=name,
                    knowledge_base_id=knowledge_base_key,
                    lock=True,
                )
                global_claim = (
                    db.query(DocumentVersion, Document)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .filter(
                        DocumentVersion.vector_collection == collection,
                        DocumentVersion.legacy_identity == identity,
                    )
                    .with_for_update()
                    .first()
                )
                claim_owned_elsewhere = bool(
                    global_claim is not None
                    and (document is None or global_claim[1].id != document.id)
                )
                now = utcnow()
                if document is None:
                    document = Document(
                        id=_new_id("doc"),
                        tenant_id=tenant,
                        knowledge_base_id=knowledge_base_key,
                        canonical_name=name,
                        owner_id=owner_id,
                        status="deleted",
                        publication_fence=1,
                        version_counter=0,
                        deleted_at=now,
                    )
                    db.add(document)
                    db.flush()
                elif document.current_version_id or document.pending_version_id:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "必须先撤销 current/pending 版本再建立 legacy tombstone",
                        status_code=409,
                    )

                if claim_owned_elsewhere:
                    document.status = "deleted"
                    document.deleted_at = document.deleted_at or now
                    document.updated_at = now
                    self._bump_revision(knowledge_base, now)
                    db.flush()
                    return RetirementResult(
                        document_id=document.id,
                        tenant_id=document.tenant_id,
                        knowledge_base_id=document.knowledge_base_id,
                        canonical_name=name,
                        found=True,
                        already_deleted=True,
                        cleanup_versions=(),
                    )

                tombstone = global_claim[0] if global_claim is not None else None
                cleanup_after = _cleanup_at(now, cleanup_grace)
                if tombstone is None:
                    tombstone = self._append_legacy_tombstone(
                        db,
                        document=document,
                        identity=identity,
                        collection=collection,
                        content_sha256=_payload_hash(
                            {
                                "schema_version": 1,
                                "legacy_tombstone": identity,
                            }
                        ),
                        source_object_key=name,
                        now=now,
                        cleanup_after=cleanup_after,
                    )
                else:
                    tombstone.status = DocumentVersionStatus.SUPERSEDED
                    tombstone.superseded_at = tombstone.superseded_at or now
                    tombstone.cleanup_after = cleanup_after
                    tombstone.index_cleaned_at = None
                    tombstone.cleanup_error_code = None
                    tombstone.updated_at = now
                document.status = "deleted"
                document.deleted_at = document.deleted_at or now
                document.updated_at = now
                self._bump_revision(knowledge_base, now)
                db.flush()
                return RetirementResult(
                    document_id=document.id,
                    tenant_id=document.tenant_id,
                    knowledge_base_id=document.knowledge_base_id,
                    canonical_name=name,
                    found=True,
                    already_deleted=True,
                    cleanup_versions=(self._version_record(tombstone),),
                )
        finally:
            db.close()

    def adopt_legacy(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        canonical_name: str,
        owner_id: int,
        legacy_identity: str,
        corpus_fingerprint: str,
        adoption_fence: int,
        source_object_key: str,
        vector_collection: str,
        chunk_count: int,
        parent_chunk_count: int,
        content_sha256: str | None = None,
        media_type: str = "",
        size_bytes: int = 0,
        index_version: str = "legacy",
    ) -> LegacyAdoptionResult:
        tenant = _required_text(tenant_id, "tenant_id", 64)
        knowledge_base_key = _required_text(knowledge_base_id, "knowledge_base_id", 64)
        name = _required_text(canonical_name, "canonical_name", 255)
        source_key = _required_text(source_object_key, "source_object_key", 512)
        collection = _required_text(vector_collection, "vector_collection", 160)
        identity = _required_text(legacy_identity, "legacy_identity", 512)
        corpus_digest = _content_hash(corpus_fingerprint)
        if adoption_fence <= 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "adoption_fence 必须为正整数",
                status_code=400,
            )
        expected_identity = legacy_source_identity(
            vector_collection=collection,
            canonical_name=name,
        )
        if identity != expected_identity:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "legacy_identity 与文档来源不匹配",
                status_code=400,
            )
        if chunk_count < 0 or parent_chunk_count < 0 or size_bytes < 0:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "legacy 计数不能为负数",
                status_code=400,
            )
        digest = (
            _content_hash(content_sha256)
            if content_sha256
            else _payload_hash(
                {
                    "schema_version": 1,
                    "tenant_id": tenant,
                    "knowledge_base_id": knowledge_base_key,
                    "legacy_identity": identity,
                }
            )
        )
        fingerprint = _payload_hash(
            {
                "schema_version": 1,
                "storage_layout": StorageLayout.LEGACY_FILENAME,
                "legacy_identity": identity,
                "vector_collection": collection,
                "index_version": index_version,
            }
        )
        db = self._session_factory()
        try:
            with db.begin():
                knowledge_base = self._knowledge_base(
                    db,
                    tenant_id=tenant,
                    knowledge_base_id=knowledge_base_key,
                    lock=True,
                )
                adoption_state = (
                    db.query(DocumentCatalogState)
                    .filter(DocumentCatalogState.tenant_id == tenant)
                    .with_for_update()
                    .first()
                )
                if (
                    adoption_state is None
                    or adoption_state.legacy_collection != collection
                    or adoption_state.legacy_knowledge_base_name != knowledge_base.name
                    or adoption_state.legacy_corpus_fingerprint != corpus_digest
                    or adoption_state.legacy_adoption_fence != adoption_fence
                    or (
                        adoption_state.legacy_knowledge_base_id is not None
                        and adoption_state.legacy_knowledge_base_id
                        != knowledge_base_key
                    )
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "legacy 接管 fencing 已失效，请重新扫描",
                        status_code=409,
                        retryable=True,
                    )
                document = self._document(
                    db,
                    tenant_id=tenant,
                    canonical_name=name,
                    knowledge_base_id=knowledge_base_key,
                    lock=True,
                )
                if document and document.owner_id != owner_id:
                    raise AppError(
                        ErrorCode.PERMISSION_DENIED,
                        "无权接管该文档",
                        status_code=403,
                    )
                now = utcnow()
                source_claim = (
                    db.query(DocumentVersion, Document)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .filter(
                        DocumentVersion.vector_collection == collection,
                        DocumentVersion.legacy_identity == identity,
                    )
                    .with_for_update()
                    .first()
                )
                if source_claim is not None:
                    claim_version, claim_document = source_claim
                    if (
                        claim_document.tenant_id != tenant
                        or document is None
                        or claim_document.id != document.id
                    ):
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "legacy 文档来源已由其他目录声明",
                            status_code=409,
                        )
                    claim_job = self._job_for_version(db, claim_version.id)

                    def claim_result(reason: str) -> LegacyAdoptionResult:
                        return LegacyAdoptionResult(
                            document=self._document_records(db, [claim_document])[0],
                            version=self._version_record(claim_version),
                            job=(
                                self._job_record(claim_job, document=claim_document)
                                if claim_job
                                else None
                            ),
                            adopted=False,
                            reason=reason,
                        )

                    if (
                        claim_version.parser_version == _LEGACY_TOMBSTONE_PARSER
                        or claim_document.deleted_at is not None
                    ):
                        return claim_result("legacy_tombstoned")
                    if claim_version.content_sha256 != digest:
                        return claim_result("legacy_content_drift")
                    if document.current_version_id or document.pending_version_id:
                        pointer = self._version_by_pointer(
                            db,
                            document,
                            document.current_version_id or document.pending_version_id,
                        )
                        return claim_result(
                            "already_adopted"
                            if (
                                pointer is not None
                                and pointer.id == claim_version.id
                                and pointer.status == DocumentVersionStatus.READY
                            )
                            else "catalog_not_empty"
                        )
                    return claim_result("legacy_identity_terminal")

                if document is None:
                    document = Document(
                        id=_new_id("doc"),
                        tenant_id=tenant,
                        knowledge_base_id=knowledge_base_key,
                        canonical_name=name,
                        owner_id=owner_id,
                        status="pending",
                        publication_fence=0,
                        version_counter=0,
                    )
                    db.add(document)
                    db.flush()
                if document.deleted_at is not None:
                    tombstone = self._append_legacy_tombstone(
                        db,
                        document=document,
                        identity=identity,
                        collection=collection,
                        content_sha256=digest,
                        source_object_key=source_key,
                        now=now,
                    )
                    self._bump_revision(knowledge_base, now)
                    return LegacyAdoptionResult(
                        document=self._document_records(db, [document])[0],
                        version=self._version_record(tombstone),
                        job=None,
                        adopted=False,
                        reason="legacy_tombstoned",
                    )
                if document.current_version_id:
                    pointer = self._version_by_pointer(
                        db,
                        document,
                        document.current_version_id,
                    )
                    if (
                        pointer is not None
                        and pointer.status == DocumentVersionStatus.READY
                        and pointer.storage_layout == StorageLayout.VERSIONED
                    ):
                        tombstone = self._append_legacy_tombstone(
                            db,
                            document=document,
                            identity=identity,
                            collection=collection,
                            content_sha256=digest,
                            source_object_key=source_key,
                            now=now,
                        )
                        document.updated_at = now
                        self._bump_revision(knowledge_base, now)
                        return LegacyAdoptionResult(
                            document=self._document_records(db, [document])[0],
                            version=self._version_record(tombstone),
                            job=None,
                            adopted=False,
                            reason="catalog_current_suppresses_legacy",
                        )
                    return LegacyAdoptionResult(
                        document=self._document_records(db, [document])[0],
                        version=(self._version_record(pointer) if pointer else None),
                        job=(
                            self._job_record(
                                self._job_for_version(db, pointer.id),
                                document=document,
                            )
                            if pointer and self._job_for_version(db, pointer.id)
                            else None
                        ),
                        adopted=False,
                        reason="catalog_not_empty",
                    )
                if document.pending_version_id:
                    pointer = self._version_by_pointer(
                        db,
                        document,
                        document.pending_version_id,
                    )
                    return LegacyAdoptionResult(
                        document=self._document_records(db, [document])[0],
                        version=(self._version_record(pointer) if pointer else None),
                        job=(
                            self._job_record(
                                self._job_for_version(db, pointer.id),
                                document=document,
                            )
                            if pointer and self._job_for_version(db, pointer.id)
                            else None
                        ),
                        adopted=False,
                        reason="catalog_not_empty",
                    )
                document.version_counter += 1
                existing = DocumentVersion(
                    id=_new_id("docver"),
                    document_id=document.id,
                    version_number=document.version_counter,
                    content_sha256=digest,
                    build_fingerprint=fingerprint,
                    source_object_key=source_key,
                    media_type=str(media_type or "")[:160],
                    size_bytes=size_bytes,
                    parser_version="legacy",
                    chunker_version="legacy",
                    embedding_model="legacy",
                    index_version=_required_text(index_version, "index_version", 64),
                    storage_layout=StorageLayout.LEGACY_FILENAME,
                    vector_collection=collection,
                    legacy_identity=identity,
                    status=DocumentVersionStatus.READY,
                    chunk_count=chunk_count,
                    parent_chunk_count=parent_chunk_count,
                    published_at=now,
                )
                db.add(existing)
                db.flush()
                job = self._job_for_version(db, existing.id)
                if job is None:
                    job = IndexJob(
                        id=_new_id("idxjob"),
                        document_version_id=existing.id,
                        status=IndexJobStatus.COMPLETED,
                        current_step="legacy_adopted",
                        progress=100,
                        max_attempts=1,
                        publication_fence=document.publication_fence + 1,
                        expected_current_version_id=None,
                        step_state_json={
                            "storage_layout": StorageLayout.LEGACY_FILENAME,
                            "legacy_identity": identity,
                            "vector_chunk_count": chunk_count,
                            "parent_chunk_count": parent_chunk_count,
                        },
                        finished_at=now,
                    )
                    db.add(job)
                document.publication_fence += 1
                document.current_version_id = existing.id
                document.pending_version_id = None
                document.status = "ready"
                document.updated_at = now
                self._bump_revision(knowledge_base, now)
                db.flush()
                return LegacyAdoptionResult(
                    document=self._document_records(db, [document])[0],
                    version=self._version_record(existing),
                    job=self._job_record(job, document=document),
                    adopted=True,
                    reason="adopted",
                )
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "legacy 目录接管并发冲突，请重试",
                status_code=409,
                retryable=True,
            ) from exc
        finally:
            db.close()

    def cleanup_candidates(
        self,
        *,
        tenant_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[CleanupCandidate]:
        cutoff = now or utcnow()
        db = self._session_factory()
        try:
            rows = (
                db.query(DocumentVersion, Document)
                .join(Document, Document.id == DocumentVersion.document_id)
                .filter(
                    Document.tenant_id == tenant_id,
                    DocumentVersion.status.in_(
                        {
                            DocumentVersionStatus.FAILED,
                            DocumentVersionStatus.SUPERSEDED,
                        }
                    ),
                    DocumentVersion.cleanup_after.is_not(None),
                    DocumentVersion.cleanup_after <= cutoff,
                    DocumentVersion.index_cleaned_at.is_(None),
                )
                .order_by(DocumentVersion.cleanup_after.asc())
                .limit(limit)
                .all()
            )
            return [
                CleanupCandidate(
                    tenant_id=document.tenant_id,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    canonical_name=document.canonical_name,
                    version=self._version_record(version),
                )
                for version, document in rows
            ]
        finally:
            db.close()

    def record_cleanup(
        self,
        *,
        document_version_id: str,
        error_code: str | None = None,
    ) -> DocumentVersionRecord:
        db = self._session_factory()
        try:
            with db.begin():
                row = (
                    db.query(DocumentVersion, Document, KnowledgeBase)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
                    .filter(DocumentVersion.id == document_version_id)
                    .with_for_update()
                    .first()
                )
                if not row:
                    raise AppError(
                        ErrorCode.NOT_FOUND, "文档版本不存在", status_code=404
                    )
                version, _document, knowledge_base = row
                if version.status not in {
                    DocumentVersionStatus.FAILED,
                    DocumentVersionStatus.SUPERSEDED,
                }:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "仅 failed/superseded 版本可以记录清理结果",
                        status_code=409,
                    )
                now = utcnow()
                if version.index_cleaned_at is not None:
                    return self._version_record(version)
                if error_code:
                    version.cleanup_error_code = _required_text(
                        error_code, "error_code", 64
                    )
                else:
                    version.index_cleaned_at = now
                    version.cleanup_error_code = None
                version.updated_at = now
                self._bump_revision(knowledge_base, now)
                db.flush()
                return self._version_record(version)
        finally:
            db.close()

    def load_retrieval_snapshot(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None = None,
    ) -> RetrievalCatalogSnapshot:
        db = self._session_factory()
        try:
            with db.begin():
                catalog_state = (
                    db.query(DocumentCatalogState)
                    .filter(DocumentCatalogState.tenant_id == tenant_id)
                    .with_for_update(read=True)
                    .first()
                )
                legacy_adoption_complete = bool(
                    catalog_state is not None
                    and catalog_state.legacy_adoption_completed_at is not None
                    and isinstance(catalog_state.legacy_corpus_fingerprint, str)
                    and _SHA256_RE.fullmatch(catalog_state.legacy_corpus_fingerprint)
                )
                legacy_collection = (
                    catalog_state.legacy_collection
                    if catalog_state is not None
                    else None
                )
                legacy_knowledge_base_id = (
                    catalog_state.legacy_knowledge_base_id
                    if catalog_state is not None
                    else None
                )
                legacy_knowledge_base_name = (
                    catalog_state.legacy_knowledge_base_name
                    if catalog_state is not None
                    else None
                )
                legacy_corpus_fingerprint = (
                    catalog_state.legacy_corpus_fingerprint
                    if legacy_adoption_complete
                    else _payload_hash(
                        {
                            "schema_version": 1,
                            "tenant_id": tenant_id,
                            "legacy_collection": legacy_collection,
                            "legacy_knowledge_base_id": legacy_knowledge_base_id,
                            "legacy_knowledge_base_name": legacy_knowledge_base_name,
                            "legacy_corpus_fingerprint": (
                                catalog_state.legacy_corpus_fingerprint
                                if catalog_state is not None
                                else None
                            ),
                            "legacy_adoption_state": (
                                "incomplete" if catalog_state is not None else "missing"
                            ),
                        }
                    )
                )
                knowledge_base_query = db.query(KnowledgeBase).filter(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.status == "active",
                )
                if knowledge_base_id is not None:
                    knowledge_base_query = knowledge_base_query.filter(
                        KnowledgeBase.id == knowledge_base_id
                    )
                active_knowledge_bases = (
                    knowledge_base_query.order_by(KnowledgeBase.id.asc())
                    .with_for_update(read=True)
                    .all()
                )
                active_knowledge_base_ids = [row.id for row in active_knowledge_bases]
                query = db.query(Document).filter(
                    Document.tenant_id == tenant_id,
                    Document.knowledge_base_id.in_(active_knowledge_base_ids),
                )
                if knowledge_base_id is not None:
                    query = query.filter(
                        Document.knowledge_base_id == knowledge_base_id
                    )
                # PostgreSQL FOR SHARE prevents a concurrent pointer swap until the
                # exact manifests have been read. SQLite ignores the clause but its
                # tests still exercise the same one-session Interface.
                documents = (
                    query.order_by(Document.id.asc()).with_for_update(read=True).all()
                )
                records = tuple(self._document_records(db, documents))
                current_versions = [
                    record.current_version
                    for record in records
                    if record.deleted_at is None
                    and record.current_version is not None
                    and record.current_version.status == DocumentVersionStatus.READY
                ]
                current_version_ids = [version.id for version in current_versions]
                manifests = (
                    db.query(IndexManifest)
                    .filter(IndexManifest.document_version_id.in_(current_version_ids))
                    .order_by(
                        IndexManifest.document_version_id.asc(),
                        IndexManifest.store_kind.asc(),
                        IndexManifest.chunk_id.asc(),
                    )
                    .all()
                    if current_version_ids
                    else []
                )
                manifests_by_version: dict[str, list[dict]] = {}
                for row in manifests:
                    manifests_by_version.setdefault(row.document_version_id, []).append(
                        {
                            "store_kind": row.store_kind,
                            "chunk_id": row.chunk_id,
                            "section_id": row.section_id,
                            "chunk_level": row.chunk_level,
                            "content_hash": row.content_hash,
                        }
                    )
                current_documents: list[dict] = []
                for record in records:
                    version = record.current_version
                    if (
                        record.deleted_at is not None
                        or version is None
                        or version.status != DocumentVersionStatus.READY
                    ):
                        continue
                    current_documents.append(
                        {
                            "document_id": record.id,
                            "canonical_name": record.canonical_name,
                            "version_id": version.id,
                            "version_number": version.version_number,
                            "content_sha256": version.content_sha256,
                            "build_fingerprint": version.build_fingerprint,
                            "index_version": version.index_version,
                            "storage_layout": version.storage_layout,
                            "vector_collection": version.vector_collection,
                            "chunk_count": version.chunk_count,
                            "parent_chunk_count": version.parent_chunk_count,
                            "manifest": manifests_by_version.get(version.id, []),
                        }
                    )
                document_ids = [record.id for record in records]
                historical_legacy_document_ids = (
                    {
                        row[0]
                        for row in db.query(DocumentVersion.document_id)
                        .filter(
                            DocumentVersion.document_id.in_(document_ids),
                            DocumentVersion.storage_layout
                            == StorageLayout.LEGACY_FILENAME,
                        )
                        .distinct()
                        .all()
                    }
                    if document_ids
                    else set()
                )
                suppressed_legacy_names = tuple(
                    sorted(
                        {
                            record.canonical_name
                            for record in records
                            if record.deleted_at is not None
                            or record.id in historical_legacy_document_ids
                            or (
                                record.current_version is not None
                                and record.current_version.status
                                == DocumentVersionStatus.READY
                                and record.current_version.storage_layout
                                == StorageLayout.VERSIONED
                            )
                        }
                    )
                )
                index_id = _payload_hash(
                    {
                        "schema_version": 1,
                        "tenant_id": tenant_id,
                        "knowledge_base_id": knowledge_base_id,
                        "current_documents": current_documents,
                        "suppressed_legacy_names": suppressed_legacy_names,
                        "legacy_adoption_complete": legacy_adoption_complete,
                        "legacy_corpus_fingerprint": legacy_corpus_fingerprint,
                        "legacy_collection": legacy_collection,
                        "legacy_knowledge_base_id": legacy_knowledge_base_id,
                        "legacy_knowledge_base_name": legacy_knowledge_base_name,
                    }
                )
                return RetrievalCatalogSnapshot(
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    documents=records,
                    index_id=index_id,
                    suppressed_legacy_names=suppressed_legacy_names,
                    legacy_adoption_complete=legacy_adoption_complete,
                    legacy_corpus_fingerprint=legacy_corpus_fingerprint,
                    legacy_collection=legacy_collection,
                    legacy_knowledge_base_id=legacy_knowledge_base_id,
                    legacy_knowledge_base_name=legacy_knowledge_base_name,
                )
        finally:
            db.close()

    def current_index_fingerprint(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None = None,
    ) -> str:
        return self.load_retrieval_snapshot(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        ).index_id
