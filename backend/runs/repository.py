from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import get_settings
from backend.db.models import ChatMessage, ChatSession, Run, User, utcnow
from backend.infra.database import SessionLocal
from backend.runs.state import ACTIVE_RUN_STATUSES, MultitaskStrategy, RunStatus


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
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RunReservation:
    run: RunRecord
    created: bool
    thread_version: int


def hash_run_request(
    message: str, *, model_name: str = "", schema_version: int = 1
) -> str:
    payload = json.dumps(
        {
            "message": message,
            "model_name": model_name,
            "schema_version": schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RunRepository:
    """Run reservation 的事务 seam：幂等、Thread fencing 与消息预留同地完成。"""

    def __init__(self, session_factory: SessionFactory = SessionLocal):
        self._session_factory = session_factory

    @staticmethod
    def _record(run: Run, thread_id: str) -> RunRecord:
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
            created_at=run.created_at.isoformat(),
            updated_at=run.updated_at.isoformat(),
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
    def _thread_query(db: Session, user_id: int, thread_id: str):
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
    ) -> ChatSession:
        thread = (
            RunRepository._thread_query(db, user.id, thread_id)
            .with_for_update()
            .first()
        )
        if thread:
            return thread
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
    ) -> RunReservation:
        key = self._validate_idempotency_key(idempotency_key)
        calculated_hash = request_hash or hash_run_request(
            message,
            model_name=model_name,
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


repository = RunRepository()
