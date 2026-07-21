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


EMPTY_MODEL_CATALOG_HASH = (
    "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)


def empty_model_snapshot() -> dict:
    return {
        "schema_version": 1,
        "catalog_hash": EMPTY_MODEL_CATALOG_HASH,
        "assignments": {},
    }


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

    threads = relationship(
        "Thread", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    model_profiles = relationship(
        "ModelProfile",
        back_populates="created_by",
        foreign_keys="ModelProfile.created_by_user_id",
    )


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("user_id", "thread_id", name="uq_user_thread"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
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

    user = relationship("User", back_populates="threads")
    messages = relationship(
        "Message",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="Message.sequence",
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
        CheckConstraint(
            "(skill_name IS NULL AND skill_version IS NULL "
            "AND skill_content_hash IS NULL AND skill_activation_source IS NULL) "
            "OR (skill_name IS NOT NULL AND skill_version IS NOT NULL "
            "AND skill_content_hash IS NOT NULL "
            "AND skill_activation_source IS NOT NULL)",
            name="ck_runs_skill_snapshot_complete",
        ),
        CheckConstraint(
            "length(model_catalog_hash) = 64",
            name="ck_runs_model_catalog_hash_length",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_ref_id: Mapped[int] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), default="default", nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), default="run", nullable=False)
    approved_tools_json: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    model_catalog_hash: Mapped[str] = mapped_column(
        CHAR(64), default=EMPTY_MODEL_CATALOG_HASH, nullable=False
    )
    model_snapshot_json: Mapped[dict] = mapped_column(
        JSON, default=empty_model_snapshot, nullable=False
    )
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
    skill_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_content_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    skill_activation_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
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

    thread = relationship("Thread", back_populates="runs")
    messages = relationship("Message", back_populates="run")
    events = relationship(
        "RunEvent", back_populates="run", cascade="all, delete-orphan"
    )
    checkpoints = relationship(
        "RunCheckpoint", back_populates="run", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "thread_ref_id", "sequence", name="uq_message_thread_sequence"
        ),
        UniqueConstraint(
            "thread_ref_id",
            "client_message_id",
            name="uq_message_client_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    thread_ref_id: Mapped[int] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
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

    thread = relationship("Thread", back_populates="messages")
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
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
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
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    user = relationship("User", back_populates="refresh_tokens")


class ModelProfile(Base):
    __tablename__ = "model_profiles"
    __table_args__ = (
        UniqueConstraint("display_name", name="uq_model_profile_display_name"),
        CheckConstraint("version >= 1", name="ck_model_profile_version_positive"),
        CheckConstraint(
            "timeout_seconds > 0 AND timeout_seconds <= 600",
            name="ck_model_profile_timeout_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(32), default="openai", nullable=False, index=True
    )
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    timeout_seconds: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), default=Decimal("30"), nullable=False
    )
    supports_stream: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_structured_output: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source: Mapped[str] = mapped_column(
        String(24), default="user", nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    created_by = relationship(
        "User",
        back_populates="model_profiles",
        foreign_keys=[created_by_user_id],
    )
    assignments = relationship("ModelAssignment", back_populates="profile")


class ModelAssignment(Base):
    __tablename__ = "model_assignments"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    profile = relationship("ModelProfile", back_populates="assignments")
    updated_by = relationship("User", foreign_keys=[updated_by_user_id])


class RagEvaluationDataset(Base):
    __tablename__ = "rag_evaluation_datasets"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_rag_evaluation_dataset_fingerprint"),
        Index("ix_rag_evaluation_datasets_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    created_by = relationship("User", foreign_keys=[created_by_user_id])
    jobs = relationship(
        "RagEvaluationJob",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )


class RagEvaluationJob(Base):
    __tablename__ = "rag_evaluation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'cancelling', 'cancelled', "
            "'succeeded', 'failed')",
            name="ck_rag_evaluation_jobs_status",
        ),
        CheckConstraint(
            "completed_cases >= 0 AND total_cases >= 1 "
            "AND completed_cases <= total_cases",
            name="ck_rag_evaluation_jobs_progress",
        ),
        CheckConstraint(
            "length(model_catalog_hash) = 64",
            name="ck_rag_evaluation_jobs_model_hash_length",
        ),
        Index("ix_rag_evaluation_jobs_status_created", "status", "created_at"),
        Index("ix_rag_evaluation_jobs_dataset_created", "dataset_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("rag_evaluation_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    baseline_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("rag_evaluation_jobs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    completed_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    gate_policy_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    model_catalog_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    model_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    dataset = relationship("RagEvaluationDataset", back_populates="jobs")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    cases = relationship(
        "RagEvaluationCaseRecord",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="RagEvaluationCaseRecord.position",
    )


class RagEvaluationCaseRecord(Base):
    __tablename__ = "rag_evaluation_cases"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "case_id",
            name="uq_rag_evaluation_case_job_case",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_rag_evaluation_cases_status",
        ),
        Index("ix_rag_evaluation_cases_job_position", "job_id", "position"),
        Index("ix_rag_evaluation_cases_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("rag_evaluation_jobs.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[str] = mapped_column(String(160), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    generated_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    judge_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    checks_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    retrieved_identity_json: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False
    )
    provider_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_error_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    job = relationship("RagEvaluationJob", back_populates="cases")


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
    vector_collection: Mapped[str] = mapped_column(
        String(160), default="", nullable=False
    )
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
    __table_args__ = (
        UniqueConstraint("run_id", "audit_key", name="uq_tool_audit_run_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    audit_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tool_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    skill_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
