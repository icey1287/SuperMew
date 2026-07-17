from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from backend.core.errors import (
    AppError,
    ErrorCode,
    PublicError,
    deserialize_public_error,
    serialize_public_error,
)
from backend.core.settings import get_settings
from backend.db.models import (
    ChatMessage,
    ChatSession,
    Run,
    RunCheckpoint,
    ToolAudit,
    User,
    utcnow,
)
from backend.events.generated.run_event_v1 import RunEventType
from backend.events.journal import append_event_in_session
from backend.infra.database import SessionLocal
from backend.runs.state import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    MultitaskStrategy,
    RunStatus,
    can_transition,
)
from backend.threads.contracts import validate_thread_id


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class RunRecord:
    id: str
    thread_id: str
    status: str
    idempotency_key: str
    request_hash: str
    multitask_strategy: str
    fencing_token: int
    user_message_id: int
    assistant_message_id: int
    supersedes_run_id: str | None
    model_name: str
    on_disconnect: str
    owner_worker_id: str | None
    lease_expires_at: str | None
    deadline_at: str | None
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    skill_name: str | None
    skill_version: str | None
    skill_content_hash: str | None
    skill_activation_source: str | None
    input_tokens: int
    output_tokens: int
    cost: str
    created_at: str
    updated_at: str
    error: dict | None = None


@dataclass(frozen=True)
class RunReservation:
    run: RunRecord
    created: bool
    thread_version: int


@dataclass(frozen=True)
class ExecutionMessage:
    role: str
    content: str


@dataclass(frozen=True)
class RunExecutionSnapshot:
    run: RunRecord
    user_db_id: int
    username: str
    role: str
    tenant_id: str
    channel: str
    approved_tools: frozenset[str]
    user_text: str
    history: tuple[ExecutionMessage, ...]
    persistent_note: str


def hash_run_request(
    message: str,
    *,
    model_name: str = "",
    tenant_id: str = "default",
    channel: str = "chat",
    approved_tools: frozenset[str] = frozenset(),
    schema_version: int = 1,
) -> str:
    payload = json.dumps(
        {
            "message": message,
            "model_name": model_name,
            "tenant_id": tenant_id,
            "channel": channel,
            "approved_tools": sorted(approved_tools),
            "schema_version": schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cancelled_public_error() -> PublicError:
    return PublicError(
        code=ErrorCode.RUN_CANCELLED,
        message="运行已由用户取消。",
        status_code=409,
        retryable=False,
        category="run",
        stage="cancellation",
    )


class RunRepository:
    """Run reservation 的事务 seam：幂等、Thread fencing 与消息预留同地完成。"""

    def __init__(self, session_factory: SessionFactory = SessionLocal):
        self._session_factory = session_factory

    @staticmethod
    def _record(run: Run, thread_id: str) -> RunRecord:
        public_error = deserialize_public_error(run.error_detail_redacted)
        return RunRecord(
            id=run.id,
            thread_id=thread_id,
            status=run.status,
            idempotency_key=run.idempotency_key,
            request_hash=run.request_hash,
            multitask_strategy=run.multitask_strategy,
            fencing_token=run.fencing_token,
            user_message_id=int(run.user_message_id or 0),
            assistant_message_id=int(run.assistant_message_id or 0),
            supersedes_run_id=run.supersedes_run_id,
            model_name=run.model_name,
            on_disconnect=run.on_disconnect,
            owner_worker_id=run.owner_worker_id,
            lease_expires_at=(
                run.lease_expires_at.isoformat() if run.lease_expires_at else None
            ),
            deadline_at=run.deadline_at.isoformat() if run.deadline_at else None,
            started_at=run.started_at.isoformat() if run.started_at else None,
            finished_at=run.finished_at.isoformat() if run.finished_at else None,
            error_code=run.error_code,
            skill_name=run.skill_name,
            skill_version=run.skill_version,
            skill_content_hash=run.skill_content_hash,
            skill_activation_source=run.skill_activation_source,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cost=str(run.cost),
            created_at=run.created_at.isoformat(),
            updated_at=run.updated_at.isoformat(),
            error=public_error.contract() if public_error else None,
        )

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        key = (value or "").strip()
        if not key or len(key) > 128:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "idempotency_key 必须为 1-128 个字符",
                status_code=400,
            )
        return key

    @staticmethod
    def _security_identifier(value: str, *, field_name: str, maximum: int) -> str:
        candidate = (value or "").strip()
        if (
            not candidate
            or len(candidate) > maximum
            or re.fullmatch(r"[a-z][a-z0-9_.:-]*", candidate) is None
        ):
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"{field_name} 不是有效的安全标识符",
                status_code=400,
            )
        return candidate

    @staticmethod
    def _approved_tools(values: frozenset[str]) -> frozenset[str]:
        try:
            normalized = frozenset(values)
        except TypeError as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "approved_tools 必须是工具名称集合",
                status_code=400,
            ) from exc
        if len(normalized) > 32 or any(
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", name) is None
            for name in normalized
        ):
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "approved_tools 包含非法工具名称",
                status_code=400,
            )
        return normalized

    @staticmethod
    def _thread_query(
        db: Session,
        user_id: int,
        thread_id: str,
    ) -> Query[ChatSession]:
        return db.query(ChatSession).filter(
            ChatSession.user_id == user_id,
            ChatSession.session_id == thread_id,
        )

    @staticmethod
    def _get_or_create_thread(
        db: Session,
        user: User,
        thread_id: str,
        *,
        title: str | None = None,
        allow_create: bool,
    ) -> ChatSession:
        thread = (
            RunRepository._thread_query(db, user.id, thread_id)
            .with_for_update()
            .first()
        )
        if thread:
            if title and not (thread.metadata_json or {}).get("title"):
                thread.metadata_json = {
                    **(thread.metadata_json or {}),
                    "title": title,
                }
            return thread
        if not allow_create:
            raise AppError(ErrorCode.NOT_FOUND, "Thread 不存在", status_code=404)
        metadata = {"title": title} if title else {}
        thread = ChatSession(
            user_id=user.id,
            session_id=thread_id,
            status="active",
            version=0,
            message_count=0,
            last_sequence=0,
            metadata_json=metadata,
        )
        db.add(thread)
        db.flush()
        return thread

    @staticmethod
    def _existing_run(
        db: Session,
        user_id: int,
        thread_ref_id: int,
        idempotency_key: str,
    ) -> Run | None:
        return (
            db.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.thread_ref_id == thread_ref_id,
                Run.idempotency_key == idempotency_key,
            )
            .first()
        )

    @staticmethod
    def _active_run(db: Session, thread_ref_id: int) -> Run | None:
        return (
            db.query(Run)
            .filter(
                Run.thread_ref_id == thread_ref_id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
            .order_by(Run.created_at.asc())
            .with_for_update()
            .first()
        )

    def _load_reservation(
        self,
        username: str,
        thread_id: str,
        idempotency_key: str,
    ) -> RunReservation | None:
        db = self._session_factory()
        try:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                return None
            thread = self._thread_query(db, user.id, thread_id).first()
            if not thread:
                return None
            run = self._existing_run(db, user.id, thread.id, idempotency_key)
            if not run:
                return None
            return RunReservation(
                run=self._record(run, thread_id),
                created=False,
                thread_version=thread.version,
            )
        finally:
            db.close()

    def reserve(
        self,
        *,
        username: str,
        thread_id: str,
        message: str,
        idempotency_key: str,
        request_hash: str | None = None,
        expected_thread_version: int | None = None,
        model_name: str = "",
        on_disconnect: str | None = None,
        multitask_strategy: MultitaskStrategy | str | None = None,
        title: str | None = None,
        tenant_id: str = "default",
        channel: str = "chat",
        approved_tools: frozenset[str] = frozenset(),
        _allow_implicit_thread: bool = False,
    ) -> RunReservation:
        thread_id = validate_thread_id(thread_id)
        key = self._validate_idempotency_key(idempotency_key)
        normalized_tenant = self._security_identifier(
            tenant_id,
            field_name="tenant_id",
            maximum=64,
        )
        normalized_channel = self._security_identifier(
            channel,
            field_name="channel",
            maximum=32,
        )
        normalized_approved_tools = self._approved_tools(approved_tools)
        calculated_hash = request_hash or hash_run_request(
            message,
            model_name=model_name,
            tenant_id=normalized_tenant,
            channel=normalized_channel,
            approved_tools=normalized_approved_tools,
        )
        settings = get_settings().runs
        strategy = MultitaskStrategy(multitask_strategy or settings.multitask_strategy)
        now = utcnow()
        db = self._session_factory()
        try:
            with db.begin():
                user = db.query(User).filter(User.username == username).first()
                if not user:
                    raise AppError(
                        ErrorCode.AUTHENTICATION_REQUIRED,
                        "用户不存在或已失效",
                        status_code=401,
                    )
                thread = self._get_or_create_thread(
                    db,
                    user,
                    thread_id,
                    title=title,
                    allow_create=_allow_implicit_thread,
                )
                existing = self._existing_run(db, user.id, thread.id, key)
                if existing:
                    if existing.request_hash != calculated_hash:
                        raise AppError(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "相同 idempotency_key 对应了不同请求",
                            status_code=409,
                        )
                    return RunReservation(
                        run=self._record(existing, thread_id),
                        created=False,
                        thread_version=thread.version,
                    )

                if (
                    expected_thread_version is not None
                    and thread.version != expected_thread_version
                ):
                    raise AppError(
                        ErrorCode.THREAD_VERSION_CONFLICT,
                        "Thread 版本已变化，请刷新后重试",
                        status_code=409,
                        safe_details={"current_version": thread.version},
                    )

                active = self._active_run(db, thread.id)
                supersedes_run_id = None
                if active and strategy == MultitaskStrategy.REJECT:
                    raise AppError(
                        ErrorCode.RUN_ACTIVE,
                        "该 Thread 已有正在执行的 Run",
                        status_code=409,
                        safe_details={"active_run_id": active.id},
                    )
                initial_status = RunStatus.PENDING.value
                if active:
                    initial_status = RunStatus.QUEUED.value
                    if strategy == MultitaskStrategy.CANCEL_PREVIOUS:
                        supersedes_run_id = active.id

                run_id = f"run_{uuid4().hex}"
                run = Run(
                    id=run_id,
                    thread_ref_id=thread.id,
                    user_id=user.id,
                    tenant_id=normalized_tenant,
                    channel=normalized_channel,
                    approved_tools_json=sorted(normalized_approved_tools),
                    status=initial_status,
                    idempotency_key=key,
                    request_hash=calculated_hash,
                    model_name=model_name,
                    on_disconnect=on_disconnect or settings.disconnect_policy,
                    multitask_strategy=strategy.value,
                    fencing_token=1,
                    supersedes_run_id=supersedes_run_id,
                    deadline_at=now
                    + timedelta(seconds=settings.default_deadline_seconds),
                    input_tokens=0,
                    output_tokens=0,
                    cost=Decimal("0"),
                    created_at=now,
                    updated_at=now,
                )
                db.add(run)
                db.flush()

                user_message = ChatMessage(
                    session_ref_id=thread.id,
                    run_id=run_id,
                    client_message_id=f"{run_id}:user",
                    sequence=thread.last_sequence + 1,
                    message_type="human",
                    content=message,
                    status="completed",
                    timestamp=now,
                    updated_at=now,
                )
                assistant_message = ChatMessage(
                    session_ref_id=thread.id,
                    run_id=run_id,
                    client_message_id=f"{run_id}:assistant",
                    sequence=thread.last_sequence + 2,
                    message_type="ai",
                    content="",
                    status="streaming"
                    if initial_status == RunStatus.PENDING
                    else "queued",
                    timestamp=now,
                    updated_at=now,
                )
                db.add_all([user_message, assistant_message])
                db.flush()

                thread.last_sequence += 2
                thread.message_count += 2
                thread.version += 2
                thread.updated_at = now
                run.user_message_id = user_message.id
                run.assistant_message_id = assistant_message.id
                run.fencing_token = thread.version
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread_id,
                    event_type=RunEventType.RUN_CREATED,
                    data={
                        "status": run.status,
                        "user_message_id": user_message.id,
                        "assistant_message_id": assistant_message.id,
                        "multitask_strategy": run.multitask_strategy,
                    },
                )
                db.flush()
                return RunReservation(
                    run=self._record(run, thread_id),
                    created=True,
                    thread_version=thread.version,
                )
        except IntegrityError as exc:
            db.rollback()
            existing = self._load_reservation(username, thread_id, key)
            if existing and existing.run.request_hash == calculated_hash:
                return existing
            raise AppError(
                ErrorCode.RUN_ACTIVE,
                "Run 并发预留冲突，请重试",
                status_code=409,
                retryable=True,
            ) from exc
        finally:
            db.close()

    def get(self, *, username: str, run_id: str) -> RunRecord:
        db = self._session_factory()
        try:
            row = (
                db.query(Run, ChatSession)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .join(User, User.id == Run.user_id)
                .filter(Run.id == run_id, User.username == username)
                .first()
            )
            if not row:
                raise AppError(
                    ErrorCode.RUN_NOT_FOUND,
                    "Run 不存在",
                    status_code=404,
                )
            run, thread = row
            return self._record(run, thread.session_id)
        finally:
            db.close()

    def get_internal(self, *, run_id: str) -> RunRecord:
        db = self._session_factory()
        try:
            row = (
                db.query(Run, ChatSession)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .filter(Run.id == run_id)
                .first()
            )
            if not row:
                raise AppError(
                    ErrorCode.RUN_NOT_FOUND,
                    "Run 不存在",
                    status_code=404,
                )
            run, thread = row
            return self._record(run, thread.session_id)
        finally:
            db.close()

    def has_durable_checkpoint(self, *, run_id: str) -> bool:
        db = self._session_factory()
        try:
            return (
                db.query(RunCheckpoint.id)
                .filter(RunCheckpoint.run_id == run_id)
                .first()
                is not None
            )
        finally:
            db.close()

    def find_pending(self, *, username: str, thread_id: str) -> RunRecord | None:
        db = self._session_factory()
        try:
            row = (
                db.query(Run, ChatSession)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .join(User, User.id == Run.user_id)
                .filter(
                    User.username == username,
                    ChatSession.session_id == thread_id,
                    Run.status == RunStatus.PENDING.value,
                )
                .order_by(Run.created_at.asc())
                .first()
            )
            if not row:
                return None
            run, thread = row
            return self._record(run, thread.session_id)
        finally:
            db.close()

    def list_pending(self, *, limit: int = 500) -> list[tuple[str, RunRecord]]:
        db = self._session_factory()
        try:
            rows = (
                db.query(Run, ChatSession, User)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .join(User, User.id == Run.user_id)
                .filter(Run.status == RunStatus.PENDING.value)
                .order_by(Run.created_at.asc())
                .limit(max(1, min(limit, 5000)))
                .all()
            )
            return [
                (user.username, self._record(run, thread.session_id))
                for run, thread, user in rows
            ]
        finally:
            db.close()

    def load_execution_snapshot(
        self,
        *,
        username: str,
        run_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> RunExecutionSnapshot:
        db = self._session_factory()
        try:
            row = (
                db.query(Run, ChatSession, User)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .join(User, User.id == Run.user_id)
                .filter(Run.id == run_id, User.username == username)
                .first()
            )
            if not row:
                raise AppError(
                    ErrorCode.RUN_NOT_FOUND,
                    "Run 不存在",
                    status_code=404,
                )
            run, thread, user = row
            self._assert_fencing(run, fencing_token)
            if (
                run.status != RunStatus.RUNNING.value
                or run.owner_worker_id != worker_id
            ):
                raise AppError(
                    ErrorCode.RUN_STATE_CONFLICT,
                    "当前 worker 不再拥有该 Run",
                    status_code=409,
                )
            user_message = (
                db.query(ChatMessage)
                .filter(
                    ChatMessage.id == run.user_message_id,
                    ChatMessage.session_ref_id == thread.id,
                    ChatMessage.run_id == run.id,
                )
                .first()
            )
            if not user_message or user_message.message_type != "human":
                raise AppError(
                    ErrorCode.RUN_STATE_CONFLICT,
                    "Run 缺少已预留的用户消息",
                    status_code=409,
                )
            history_rows = (
                db.query(ChatMessage)
                .filter(
                    ChatMessage.session_ref_id == thread.id,
                    ChatMessage.sequence < user_message.sequence,
                    ChatMessage.status == "completed",
                )
                .order_by(ChatMessage.sequence.asc())
                .all()
            )
            return RunExecutionSnapshot(
                run=self._record(run, thread.session_id),
                user_db_id=user.id,
                username=user.username,
                role=user.role,
                tenant_id=run.tenant_id,
                channel=run.channel,
                approved_tools=frozenset(run.approved_tools_json or ()),
                user_text=user_message.content,
                history=tuple(
                    ExecutionMessage(role=item.message_type, content=item.content)
                    for item in history_rows
                    if item.message_type in {"human", "ai", "system"}
                ),
                persistent_note=str(
                    (thread.metadata_json or {}).get("persistent_note") or ""
                ),
            )
        finally:
            db.close()

    def pin_skill_activation(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
        name: str,
        version: str,
        content_hash: str,
        source: str,
    ) -> RunRecord:
        """Persist the immutable Skill snapshot selected by the current owner."""

        db = self._session_factory()
        try:
            row = (
                db.query(Run, ChatSession)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .filter(Run.id == run_id)
                .with_for_update()
                .first()
            )
            if not row:
                raise AppError(
                    ErrorCode.RUN_NOT_FOUND,
                    "Run 不存在",
                    status_code=404,
                )
            run, thread = row
            self._assert_fencing(run, fencing_token)
            if (
                run.status != RunStatus.RUNNING.value
                or run.owner_worker_id != worker_id
            ):
                raise AppError(
                    ErrorCode.RUN_STATE_CONFLICT,
                    "当前 worker 不再拥有该 Run",
                    status_code=409,
                )

            existing = (
                run.skill_name,
                run.skill_version,
                run.skill_content_hash,
                run.skill_activation_source,
            )
            requested = (name, version, content_hash, source)
            if any(existing):
                if existing != requested:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "Run 已固定为另一 Skill 快照",
                        status_code=409,
                    )
            else:
                run.skill_name = name
                run.skill_version = version
                run.skill_content_hash = content_hash
                run.skill_activation_source = source
                db.commit()
                db.refresh(run)
            return self._record(run, thread.session_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def record_tool_audit(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
        audit_key: str,
        tool_call_id: str | None,
        tool_name: str,
        tool_version: str,
        decision: str,
        reason_code: str | None,
        policy_version: str | None,
        policy_hash: str | None,
        success: bool,
        error_code: str | None,
        duration_ms: int,
        result_size: int,
        metadata: dict | None = None,
    ) -> None:
        """Persist a metadata-only tool decision under the current Run fence."""

        db = self._session_factory()
        try:
            if len(audit_key) != 64 or any(
                character not in "0123456789abcdef" for character in audit_key
            ):
                raise ValueError("audit_key must be a lowercase SHA-256 digest")
            row = (
                db.query(Run, ChatSession)
                .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                .filter(Run.id == run_id)
                .with_for_update()
                .first()
            )
            if not row:
                raise AppError(
                    ErrorCode.RUN_NOT_FOUND,
                    "Run 不存在",
                    status_code=404,
                )
            run, thread = row
            self._assert_fencing(run, fencing_token)
            if (
                run.status != RunStatus.RUNNING.value
                or run.owner_worker_id != worker_id
            ):
                raise AppError(
                    ErrorCode.RUN_STATE_CONFLICT,
                    "当前 worker 不再拥有该 Run",
                    status_code=409,
                )
            normalized_tool_call_id = tool_call_id[:128] if tool_call_id else None
            normalized_tool_name = tool_name[:128]
            normalized_tool_version = tool_version[:64]
            normalized_decision = decision[:32]
            normalized_reason_code = reason_code[:64] if reason_code else None
            normalized_policy_version = policy_version[:64] if policy_version else None
            normalized_policy_hash = policy_hash or None
            if normalized_policy_hash is not None and (
                len(normalized_policy_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in normalized_policy_hash
                )
            ):
                raise ValueError("policy_hash must be a lowercase SHA-256 digest")
            normalized_success = bool(success)
            normalized_error_code = error_code[:64] if error_code else None
            normalized_duration_ms = max(int(duration_ms), 0)
            normalized_result_size = max(int(result_size), 0)
            safe_metadata = dict(metadata or {})
            safe_metadata.update(
                {
                    "skill_name": run.skill_name or "",
                    "skill_version": run.skill_version or "",
                }
            )
            expected = (
                normalized_tool_call_id,
                normalized_tool_name,
                normalized_tool_version,
                run.skill_name or "",
                normalized_decision,
                normalized_reason_code,
                normalized_policy_version,
                normalized_policy_hash,
                normalized_success,
                normalized_error_code,
                normalized_duration_ms,
                normalized_result_size,
                safe_metadata,
            )
            existing = (
                db.query(ToolAudit)
                .filter(
                    ToolAudit.run_id == run.id,
                    ToolAudit.audit_key == audit_key,
                )
                .first()
            )
            if existing is not None:
                actual = (
                    existing.tool_call_id,
                    existing.tool_name,
                    existing.tool_version,
                    existing.skill_name,
                    existing.decision,
                    existing.reason_code,
                    existing.policy_version,
                    existing.policy_hash,
                    existing.success,
                    existing.error_code,
                    existing.duration_ms,
                    existing.result_size,
                    existing.metadata_json,
                )
                if actual != expected:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "重复工具审计键与既有结果不一致",
                        status_code=409,
                    )
                return
            db.add(
                ToolAudit(
                    user_id=run.user_id,
                    thread_id=thread.session_id,
                    run_id=run.id,
                    audit_key=audit_key,
                    tool_call_id=normalized_tool_call_id,
                    tool_name=normalized_tool_name,
                    tool_version=normalized_tool_version,
                    skill_name=run.skill_name or "",
                    decision=normalized_decision,
                    reason_code=normalized_reason_code,
                    policy_version=normalized_policy_version,
                    policy_hash=normalized_policy_hash,
                    success=normalized_success,
                    error_code=normalized_error_code,
                    duration_ms=normalized_duration_ms,
                    result_size=normalized_result_size,
                    metadata_json=safe_metadata,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _assert_fencing(run: Run, fencing_token: int | None) -> None:
        if fencing_token is not None and run.fencing_token != fencing_token:
            raise AppError(
                ErrorCode.RUN_STATE_CONFLICT,
                "Run fencing token 已失效",
                status_code=409,
                safe_details={"current_fencing_token": run.fencing_token},
            )

    @staticmethod
    def _transition(run: Run, target: RunStatus | str) -> None:
        target_value = RunStatus(target).value
        if not can_transition(run.status, target_value):
            raise AppError(
                ErrorCode.RUN_STATE_CONFLICT,
                f"Run 不能从 {run.status} 转换到 {target_value}",
                status_code=409,
            )
        run.status = target_value
        run.updated_at = utcnow()

    def claim(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: int | None = None,
    ) -> RunRecord:
        now = utcnow()
        lease_seconds = lease_seconds or get_settings().worker.lease_seconds
        db = self._session_factory()
        try:
            with db.begin():
                run = db.query(Run).filter(Run.id == run_id).with_for_update().first()
                if not run:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                thread = (
                    db.query(ChatSession)
                    .filter(ChatSession.id == run.thread_ref_id)
                    .first()
                )
                if run.status == RunStatus.RUNNING.value:
                    if run.owner_worker_id == worker_id and (
                        not run.lease_expires_at or run.lease_expires_at >= now
                    ):
                        return self._record(run, thread.session_id)
                    if run.lease_expires_at and run.lease_expires_at >= now:
                        raise AppError(
                            ErrorCode.RUN_ACTIVE,
                            "Run 已被其他 worker 持有",
                            status_code=409,
                        )
                elif run.status != RunStatus.PENDING.value:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        f"状态为 {run.status} 的 Run 不能领取",
                        status_code=409,
                    )
                self._transition(run, RunStatus.RUNNING)
                run.owner_worker_id = worker_id
                run.fencing_token += 1
                run.lease_expires_at = now + timedelta(seconds=lease_seconds)
                run.started_at = run.started_at or now
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.RUN_STARTED,
                    data={
                        "status": run.status,
                        "worker_id": worker_id,
                        "fencing_token": run.fencing_token,
                    },
                )
                db.flush()
                return self._record(run, thread.session_id)
        finally:
            db.close()

    def heartbeat(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int | None = None,
    ) -> RunRecord:
        lease_seconds = lease_seconds or get_settings().worker.lease_seconds
        db = self._session_factory()
        try:
            with db.begin():
                run = db.query(Run).filter(Run.id == run_id).with_for_update().first()
                if not run:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                self._assert_fencing(run, fencing_token)
                if run.owner_worker_id != worker_id or run.status not in {
                    RunStatus.RUNNING.value,
                    RunStatus.CANCELLING.value,
                }:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "当前 worker 不再拥有该 Run",
                        status_code=409,
                    )
                run.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
                run.updated_at = utcnow()
                thread = (
                    db.query(ChatSession)
                    .filter(ChatSession.id == run.thread_ref_id)
                    .first()
                )
                return self._record(run, thread.session_id)
        finally:
            db.close()

    def set_waiting_input(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> RunRecord:
        db = self._session_factory()
        try:
            with db.begin():
                run = db.query(Run).filter(Run.id == run_id).with_for_update().first()
                if not run:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                self._assert_fencing(run, fencing_token)
                if run.owner_worker_id != worker_id:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "当前 worker 不再拥有该 Run",
                        status_code=409,
                    )
                self._transition(run, RunStatus.WAITING_INPUT)
                run.owner_worker_id = None
                run.lease_expires_at = None
                thread = (
                    db.query(ChatSession)
                    .filter(ChatSession.id == run.thread_ref_id)
                    .first()
                )
                if run.assistant_message_id:
                    message = (
                        db.query(ChatMessage)
                        .filter(ChatMessage.id == run.assistant_message_id)
                        .first()
                    )
                    if message:
                        message.status = "waiting_input"
                        message.updated_at = utcnow()
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.RUN_WAITING_INPUT,
                    data={"status": run.status},
                )
                return self._record(run, thread.session_id)
        finally:
            db.close()

    def mark_cancelling(self, *, username: str, run_id: str) -> RunRecord:
        db = self._session_factory()
        try:
            with db.begin():
                row = (
                    db.query(Run, ChatSession)
                    .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                    .join(User, User.id == Run.user_id)
                    .filter(Run.id == run_id, User.username == username)
                    .with_for_update()
                    .first()
                )
                if not row:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                run, thread = row
                if (
                    run.status in TERMINAL_RUN_STATUSES
                    or run.status == RunStatus.CANCELLING
                ):
                    return self._record(run, thread.session_id)
                self._transition(run, RunStatus.CANCELLING)
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.WARNING_CREATED,
                    data={
                        "code": "CANCEL_REQUESTED",
                        "message": "用户已请求停止运行",
                    },
                )
                return self._record(run, thread.session_id)
        finally:
            db.close()

    @staticmethod
    def _message_status(target: str, partial: bool) -> str:
        if partial:
            return "incomplete"
        if target == RunStatus.SUCCEEDED.value:
            return "completed"
        if target == RunStatus.CANCELLED.value:
            return "cancelled"
        return "failed"

    def _promote_next_queued(
        self, db: Session, thread: ChatSession, now: datetime
    ) -> None:
        queued = (
            db.query(Run)
            .filter(
                Run.thread_ref_id == thread.id,
                Run.status == RunStatus.QUEUED.value,
            )
            .order_by(Run.created_at.asc())
            .with_for_update()
            .first()
        )
        if not queued:
            return
        queued.status = RunStatus.PENDING.value
        queued.updated_at = now
        if queued.assistant_message_id:
            message = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == queued.assistant_message_id)
                .first()
            )
            if message:
                message.status = "streaming"
                message.updated_at = now

    def finalize(
        self,
        *,
        run_id: str,
        target_status: RunStatus | str,
        content: str,
        fencing_token: int | None = None,
        error_code: str | None = None,
        error_detail_redacted: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: Decimal | str | float = Decimal("0"),
        rag_trace: dict | None = None,
        partial: bool = False,
        _lease_expired_before: datetime | None = None,
    ) -> RunRecord:
        target = RunStatus(target_status).value
        if target not in TERMINAL_RUN_STATUSES:
            raise ValueError("finalize target must be terminal")
        now = utcnow()
        db = self._session_factory()
        try:
            with db.begin():
                run = db.query(Run).filter(Run.id == run_id).with_for_update().first()
                if not run:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                thread = (
                    db.query(ChatSession)
                    .filter(ChatSession.id == run.thread_ref_id)
                    .with_for_update()
                    .first()
                )
                assistant = (
                    db.query(ChatMessage)
                    .filter(ChatMessage.id == run.assistant_message_id)
                    .with_for_update()
                    .first()
                )
                self._assert_fencing(run, fencing_token)
                if _lease_expired_before is not None and (
                    run.status
                    not in {RunStatus.RUNNING.value, RunStatus.CANCELLING.value}
                    or run.lease_expires_at is None
                    or run.lease_expires_at >= _lease_expired_before
                ):
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "Run lease 已续期或已被其他 worker 接管",
                        status_code=409,
                    )
                if run.status in TERMINAL_RUN_STATUSES:
                    if run.status != target:
                        raise AppError(
                            ErrorCode.RUN_STATE_CONFLICT,
                            f"Run 已终结为 {run.status}",
                            status_code=409,
                        )
                    return self._record(run, thread.session_id)
                if (
                    run.status == RunStatus.CANCELLING.value
                    and target != RunStatus.CANCELLED.value
                ):
                    cancellation = _cancelled_public_error()
                    target = RunStatus.CANCELLED.value
                    content = content if partial else cancellation.message
                    error_code = str(cancellation.code)
                    error_detail_redacted = serialize_public_error(cancellation)
                    partial = True
                self._transition(run, target)
                run.finished_at = now
                run.lease_expires_at = None
                run.error_code = error_code
                run.error_detail_redacted = error_detail_redacted
                public_error = deserialize_public_error(error_detail_redacted)
                run.input_tokens = max(0, input_tokens)
                run.output_tokens = max(0, output_tokens)
                run.cost = Decimal(str(cost))
                if assistant:
                    assistant.content = content
                    assistant.status = self._message_status(target, partial)
                    assistant.rag_trace = rag_trace
                    assistant.updated_at = now
                thread.updated_at = now
                if input_tokens or output_tokens or Decimal(str(cost)) != Decimal("0"):
                    append_event_in_session(
                        db,
                        run=run,
                        thread_id=thread.session_id,
                        event_type=RunEventType.USAGE_UPDATED,
                        data={
                            "input_tokens": run.input_tokens,
                            "output_tokens": run.output_tokens,
                            "cost": str(run.cost),
                        },
                    )
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.MESSAGE_COMPLETED,
                    data={
                        "message_id": run.assistant_message_id,
                        "content": content,
                        "status": (
                            assistant.status
                            if assistant
                            else self._message_status(target, partial)
                        ),
                        "rag_trace": rag_trace,
                    },
                )
                terminal_type = {
                    RunStatus.SUCCEEDED.value: RunEventType.RUN_COMPLETED,
                    RunStatus.FAILED.value: RunEventType.RUN_FAILED,
                    RunStatus.CANCELLED.value: RunEventType.RUN_CANCELLED,
                }[target]
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=terminal_type,
                    data={
                        "status": run.status,
                        "error_code": run.error_code,
                        "error": public_error.contract() if public_error else None,
                        "assistant_message_id": run.assistant_message_id,
                    },
                )
                db.flush()
                self._promote_next_queued(db, thread, now)
                db.flush()
                return self._record(run, thread.session_id)
        finally:
            db.close()

    def reconcile_orphans(self, *, now: datetime | None = None) -> list[str]:
        now = now or utcnow()
        db = self._session_factory()
        recovered: list[str] = []
        try:
            candidates = (
                db.query(Run.id, Run.fencing_token)
                .filter(
                    Run.status.in_(
                        [RunStatus.RUNNING.value, RunStatus.CANCELLING.value]
                    ),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at < now,
                )
                .all()
            )
        finally:
            db.close()
        for run_id, fencing_token in candidates:
            try:
                self.finalize(
                    run_id=run_id,
                    target_status=RunStatus.FAILED,
                    content="运行所属 worker 已失联，任务未完成。",
                    error_code="ORPHAN_RUN",
                    error_detail_redacted=serialize_public_error(
                        PublicError(
                            code="ORPHAN_RUN",
                            message="运行所属 worker 已失联，任务未完成。",
                            status_code=503,
                            retryable=True,
                            category="run",
                            stage="ownership",
                        )
                    ),
                    fencing_token=fencing_token,
                    partial=True,
                    _lease_expired_before=now,
                )
                recovered.append(run_id)
            except AppError:
                continue
        return recovered


repository = RunRepository()
