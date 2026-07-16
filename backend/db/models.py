from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infra.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    sessions = relationship(
        "ChatSession", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )


class ChatSession(Base):
    """对外称 Thread；保留旧表名以无损迁移历史数据。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "session_id", name="uq_user_session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), default="active", nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    user = relationship("User", back_populates="sessions")
    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.sequence",
    )
    runs = relationship("Run", back_populates="thread", cascade="all, delete-orphan")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "thread_ref_id",
            "idempotency_key",
            name="uq_run_user_thread_idempotency",
        ),
        UniqueConstraint("assistant_message_id", name="uq_run_assistant_message"),
        Index(
            "uq_runs_one_active_per_thread",
            "thread_ref_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running', 'waiting_input', 'cancelling')"
            ),
            sqlite_where=text(
                "status IN ('pending', 'running', 'waiting_input', 'cancelling')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_ref_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    on_disconnect: Mapped[str] = mapped_column(
        String(16), default="continue", nullable=False
    )
    multitask_strategy: Mapped[str] = mapped_column(
        String(24), default="reject", nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    user_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assistant_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supersedes_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), default=Decimal("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    thread = relationship("ChatSession", back_populates="runs")
    messages = relationship("ChatMessage", back_populates="run")
    events = relationship(
        "RunEvent", back_populates="run", cascade="all, delete-orphan"
    )
    checkpoints = relationship(
        "RunCheckpoint", back_populates="run", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_ref_id", "sequence", name="uq_chat_message_thread_sequence"
        ),
        UniqueConstraint(
            "session_ref_id",
            "client_message_id",
            name="uq_chat_message_client_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_ref_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default="completed", nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    rag_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    session = relationship("ChatSession", back_populates="messages")
    run = relationship("Run", back_populates="messages")


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    run = relationship("Run", back_populates="events")


class RunCheckpoint(Base):
    __tablename__ = "run_checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "checkpoint_id", name="uq_run_checkpoint"),
        UniqueConstraint(
            "run_id",
            "resume_idempotency_key",
            name="uq_run_checkpoint_resume_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_ref_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    hitl_token: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    interrupt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resume_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    resume_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    next_nodes_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    run = relationship("Run", back_populates="checkpoints")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    user = relationship("User", back_populates="refresh_tokens")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default="default", nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    catalog_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    documents = relationship(
        "Document", back_populates="knowledge_base", cascade="all, delete-orphan"
    )


class DocumentCatalogState(Base):
    __tablename__ = "document_catalog_states"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legacy_collection: Mapped[str] = mapped_column(String(160), nullable=False)
    legacy_knowledge_base_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), nullable=True
    )
    legacy_knowledge_base_name: Mapped[str] = mapped_column(String(160), nullable=False)
    legacy_adoption_fence: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    legacy_adoption_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    legacy_corpus_fingerprint: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id", "canonical_name", name="uq_document_canonical_name"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default="default", nullable=False, index=True
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            name="fk_documents_current_version",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    pending_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            name="fk_documents_pending_version",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    publication_fence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    versions = relationship(
        "DocumentVersion",
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="DocumentVersion.document_id",
    )
    current_version = relationship(
        "DocumentVersion",
        foreign_keys=[current_version_id],
        post_update=True,
    )
    pending_version = relationship(
        "DocumentVersion",
        foreign_keys=[pending_version_id],
        post_update=True,
    )
    retirement_jobs = relationship(
        "DocumentRetirementJob",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        Index(
            "uq_document_content_build_active",
            "document_id",
            "content_sha256",
            "build_fingerprint",
            unique=True,
            postgresql_where=text(
                "status IN ('uploaded', 'parsing', 'indexing', 'staged', 'ready')"
            ),
            sqlite_where=text(
                "status IN ('uploaded', 'parsing', 'indexing', 'staged', 'ready')"
            ),
        ),
        UniqueConstraint(
            "document_id", "version_number", name="uq_document_version_number"
        ),
        UniqueConstraint(
            "vector_collection",
            "legacy_identity",
            name="uq_legacy_source_identity",
        ),
        Index("ix_document_versions_cleanup_after", "cleanup_after"),
        CheckConstraint(
            "status IN ('uploaded', 'parsing', 'indexing', 'staged', "
            "'ready', 'failed', 'superseded')",
            name="ck_document_versions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    build_fingerprint: Mapped[str] = mapped_column(CHAR(64), default="", nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parser_version: Mapped[str] = mapped_column(
        String(64), default="v1", nullable=False
    )
    chunker_version: Mapped[str] = mapped_column(
        String(64), default="v1", nullable=False
    )
    embedding_model: Mapped[str] = mapped_column(
        String(160), default="", nullable=False
    )
    index_version: Mapped[str] = mapped_column(String(64), default="v1", nullable=False)
    storage_layout: Mapped[str] = mapped_column(
        String(32), default="versioned", nullable=False
    )
    vector_collection: Mapped[str] = mapped_column(
        String(160), default="", nullable=False
    )
    legacy_identity: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="uploaded", nullable=False, index=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parent_chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cleanup_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    index_cleaned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cleanup_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    document = relationship(
        "Document", back_populates="versions", foreign_keys=[document_id]
    )
    jobs = relationship(
        "IndexJob", back_populates="document_version", cascade="all, delete-orphan"
    )
    cleanup_job = relationship(
        "DocumentCleanupJob",
        back_populates="document_version",
        cascade="all, delete-orphan",
        uselist=False,
    )
    manifests = relationship(
        "IndexManifest",
        back_populates="document_version",
        cascade="all, delete-orphan",
    )


class IndexJob(Base):
    __tablename__ = "index_jobs"
    __table_args__ = (
        UniqueConstraint("document_version_id", name="uq_index_job_document_version"),
        Index(
            "ix_index_jobs_claim_ready",
            "status",
            "next_retry_at",
            "created_at",
        ),
        Index(
            "ix_index_jobs_claim_expired",
            "status",
            "lease_expires_at",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'staged', "
            "'completed', 'failed', 'cancelled', 'dead_letter')",
            name="ck_index_jobs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    current_step: Mapped[str] = mapped_column(
        String(64), default="uploaded", nullable=False
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    publication_fence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    execution_fence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    expected_current_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_state_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    document_version = relationship("DocumentVersion", back_populates="jobs")


class DocumentCleanupJob(Base):
    __tablename__ = "document_cleanup_jobs"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            name="uq_document_cleanup_job_document_version",
        ),
        Index(
            "ix_document_cleanup_jobs_claim_ready",
            "status",
            "next_retry_at",
            "created_at",
        ),
        Index(
            "ix_document_cleanup_jobs_claim_expired",
            "status",
            "lease_expires_at",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', "
            "'completed', 'dead_letter')",
            name="ck_document_cleanup_jobs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    current_step: Mapped[str] = mapped_column(
        String(64), default="pending", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_fence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_state_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    document_version = relationship("DocumentVersion", back_populates="cleanup_job")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        Index(
            "ix_worker_heartbeats_readiness",
            "worker_kind",
            "status",
            "heartbeat_at",
        ),
        CheckConstraint(
            "status IN ('starting', 'running', 'draining', 'stopped')",
            name="ck_worker_heartbeats_status",
        ),
    )

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    worker_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="starting", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class DocumentRetirementJob(Base):
    __tablename__ = "document_retirement_jobs"
    __table_args__ = (
        Index(
            "ix_document_retirement_jobs_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    publication_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cleanup_version_ids_json: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    document = relationship("Document", back_populates="retirement_jobs")


class IndexManifest(Base):
    __tablename__ = "index_manifests"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "store_kind",
            "chunk_id",
            name="uq_index_manifest_chunk",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[str] = mapped_column(String(512), nullable=False)
    store_kind: Mapped[str] = mapped_column(
        String(32), default="vector", nullable=False
    )
    section_id: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    chunk_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    document_version = relationship("DocumentVersion", back_populates="manifests")


class ParentChunk(Base):
    __tablename__ = "parent_chunks"

    chunk_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), default="default", nullable=False, index=True
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(64), default="", nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        String(64), default="", nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(64), default="", nullable=False, index=True
    )
    section_id: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    index_version: Mapped[str] = mapped_column(String(64), default="v1", nullable=False)
    acl_tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), default="", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parent_chunk_id: Mapped[str] = mapped_column(
        String(512), default="", nullable=False
    )
    root_chunk_id: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    chunk_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class TransactionOutbox(Base):
    __tablename__ = "transaction_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class ToolAudit(Base):
    __tablename__ = "tool_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
