from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import and_, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session
from sqlalchemy.sql.elements import ColumnElement

from backend.core.errors import AppError, ErrorCode
from backend.db.models import Message, Thread, Run, User, utcnow
from backend.runs.state import TERMINAL_RUN_STATUSES, RunStatus
from backend.infra.database import SessionLocal
from backend.schemas.rag import normalize_rag_trace


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class MessageAppend:
    role: str
    content: str
    status: str = "completed"
    run_id: str | None = None
    client_message_id: str | None = None
    content_json: dict | None = None
    rag_trace: dict | None = None


@dataclass(frozen=True)
class MessageRecord:
    id: int
    thread_id: str
    sequence: int
    role: str
    content: str
    status: str
    run_id: str | None
    client_message_id: str | None
    timestamp: datetime
    updated_at: datetime
    rag_trace: dict | None


@dataclass(frozen=True)
class UserAccessSnapshot:
    user_db_id: int
    username: str
    role: str


@dataclass(frozen=True)
class ThreadSummaryRecord:
    thread_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    version: int
    thread_status: str
    active_run_id: str | None
    active_run_status: str | None


def _active_run_priority() -> ColumnElement[int]:
    return case(
        (Run.status == RunStatus.RUNNING.value, 0),
        (Run.status == RunStatus.WAITING_INPUT.value, 1),
        (Run.status == RunStatus.CANCELLING.value, 2),
        (Run.status == RunStatus.PENDING.value, 3),
        (Run.status == RunStatus.QUEUED.value, 4),
        else_=5,
    )


class ThreadRepository:
    """Thread 与消息 journal 的唯一持久化 interface。"""

    def __init__(self, session_factory: SessionFactory = SessionLocal):
        self._session_factory = session_factory

    @staticmethod
    def _user(db: Session, username: str) -> User | None:
        return db.query(User).filter(User.username == username).first()

    def current_user_access(self, username: str) -> UserAccessSnapshot:
        """Read the current database role without trusting request-token claims."""

        db = self._session_factory()
        try:
            user = self._user(db, username)
            if user is None:
                raise AppError(
                    ErrorCode.AUTHENTICATION_REQUIRED,
                    "用户不存在或已失效",
                    status_code=401,
                )
            return UserAccessSnapshot(
                user_db_id=user.id,
                username=user.username,
                role=user.role,
            )
        finally:
            db.close()

    @staticmethod
    def _thread_query(
        db: Session,
        user_id: int,
        thread_id: str,
    ) -> Query[Thread]:
        return db.query(Thread).filter(
            Thread.user_id == user_id,
            Thread.thread_id == thread_id,
        )

    def _get_or_create_thread(
        self,
        db: Session,
        user: User,
        thread_id: str,
        *,
        metadata: dict | None = None,
        lock: bool = False,
    ) -> Thread:
        query = self._thread_query(db, user.id, thread_id)
        if lock:
            query = query.with_for_update()
        thread = query.first()
        if thread:
            return thread
        thread = Thread(
            user_id=user.id,
            thread_id=thread_id,
            metadata_json=metadata or {},
            status="active",
            version=0,
            message_count=0,
            last_sequence=0,
        )
        db.add(thread)
        db.flush()
        return thread

    @staticmethod
    def _record(message: Message, thread_id: str) -> MessageRecord:
        return MessageRecord(
            id=message.id,
            thread_id=thread_id,
            sequence=message.sequence,
            role=message.message_type,
            content=message.content,
            status=message.status,
            run_id=message.run_id,
            client_message_id=message.client_message_id,
            timestamp=message.timestamp,
            updated_at=message.updated_at,
            rag_trace=normalize_rag_trace(message.rag_trace),
        )

    @staticmethod
    def _assert_version(thread: Thread, expected_version: int | None) -> None:
        if expected_version is not None and thread.version != expected_version:
            raise AppError(
                ErrorCode.CONFLICT,
                "Thread 已被其他请求更新，请刷新后重试",
                status_code=409,
                safe_details={"current_version": thread.version},
            )

    def append_message(
        self,
        username: str,
        thread_id: str,
        message: MessageAppend,
        *,
        expected_version: int | None = None,
        metadata: dict | None = None,
    ) -> MessageRecord | None:
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                if not user:
                    return None
                thread = self._get_or_create_thread(
                    db,
                    user,
                    thread_id,
                    metadata=metadata,
                    lock=True,
                )
                self._assert_version(thread, expected_version)
                if message.client_message_id:
                    existing = (
                        db.query(Message)
                        .filter(
                            Message.thread_ref_id == thread.id,
                            Message.client_message_id == message.client_message_id,
                        )
                        .first()
                    )
                    if existing:
                        return self._record(existing, thread_id)

                now = utcnow()
                row = Message(
                    thread_ref_id=thread.id,
                    run_id=message.run_id,
                    client_message_id=message.client_message_id,
                    sequence=thread.last_sequence + 1,
                    message_type=message.role,
                    content=message.content,
                    content_json=message.content_json,
                    status=message.status,
                    timestamp=now,
                    updated_at=now,
                    rag_trace=normalize_rag_trace(message.rag_trace),
                )
                db.add(row)
                thread.last_sequence = row.sequence
                thread.message_count += 1
                thread.version += 1
                thread.updated_at = now
                if metadata:
                    thread.metadata_json = {**(thread.metadata_json or {}), **metadata}
                db.flush()
                return self._record(row, thread_id)
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "消息序号或幂等键冲突，请重试",
                status_code=409,
                retryable=True,
            ) from exc
        finally:
            db.close()

    def create_assistant_placeholder(
        self,
        username: str,
        thread_id: str,
        run_id: str,
        *,
        expected_version: int | None = None,
    ) -> MessageRecord | None:
        return self.append_message(
            username,
            thread_id,
            MessageAppend(
                role="ai",
                content="",
                status="streaming",
                run_id=run_id,
                client_message_id=f"{run_id}:assistant",
            ),
            expected_version=expected_version,
        )

    def finalize_message(
        self,
        username: str,
        thread_id: str,
        message_id: int,
        *,
        content: str,
        status: str = "completed",
        rag_trace: dict | None = None,
    ) -> MessageRecord | None:
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                if not user:
                    return None
                thread = (
                    self._thread_query(db, user.id, thread_id).with_for_update().first()
                )
                if not thread:
                    return None
                row = (
                    db.query(Message)
                    .filter(
                        Message.id == message_id,
                        Message.thread_ref_id == thread.id,
                    )
                    .with_for_update()
                    .first()
                )
                if not row:
                    return None
                normalized_trace = normalize_rag_trace(rag_trace)
                if (
                    row.content == content
                    and row.status == status
                    and row.rag_trace == normalized_trace
                ):
                    return self._record(row, thread_id)
                row.content = content
                row.status = status
                row.rag_trace = normalized_trace
                row.updated_at = utcnow()
                thread.updated_at = row.updated_at
                db.flush()
                return self._record(row, thread_id)
        finally:
            db.close()

    def list_messages_before(
        self,
        username: str,
        thread_id: str,
        *,
        before: int | None,
        limit: int,
    ) -> list[MessageRecord] | None:
        """Return one newest-first Message window, or ``None`` when not owned."""

        db = self._session_factory()
        try:
            user = self._user(db, username)
            if not user:
                return None
            thread = self._thread_query(db, user.id, thread_id).first()
            if not thread:
                return None
            query = db.query(Message).filter(Message.thread_ref_id == thread.id)
            if before is not None:
                query = query.filter(Message.sequence < before)
            rows = (
                query.order_by(Message.sequence.desc())
                .limit(max(1, min(limit, 501)))
                .all()
            )
            return [self._record(row, thread_id) for row in rows]
        finally:
            db.close()

    @staticmethod
    def _summary_record(
        thread: Thread,
        active_run: Run | None,
    ) -> ThreadSummaryRecord:
        return ThreadSummaryRecord(
            thread_id=thread.thread_id,
            title=str((thread.metadata_json or {}).get("title") or thread.thread_id),
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            message_count=thread.message_count,
            version=thread.version,
            thread_status=thread.status,
            active_run_id=active_run.id if active_run is not None else None,
            active_run_status=active_run.status if active_run is not None else None,
        )

    def create_thread(
        self,
        *,
        username: str,
        thread_id: str,
        title: str | None = None,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                if user is None:
                    raise AppError(
                        ErrorCode.AUTHENTICATION_REQUIRED,
                        "用户不存在或已失效",
                        status_code=401,
                    )
                thread = self._get_or_create_thread(
                    db,
                    user,
                    thread_id,
                    metadata={"title": title} if title else None,
                    lock=True,
                )
                if title and not (thread.metadata_json or {}).get("title"):
                    thread.metadata_json = {
                        **(thread.metadata_json or {}),
                        "title": title,
                    }
        finally:
            db.close()

    def get_thread_summary(
        self,
        username: str,
        thread_id: str,
    ) -> ThreadSummaryRecord | None:
        db = self._session_factory()
        try:
            user = self._user(db, username)
            if user is None:
                return None
            row = (
                db.query(Thread, Run)
                .outerjoin(
                    Run,
                    and_(
                        Run.thread_ref_id == Thread.id,
                        Run.status.notin_(tuple(TERMINAL_RUN_STATUSES)),
                    ),
                )
                .filter(
                    Thread.user_id == user.id,
                    Thread.thread_id == thread_id,
                )
                .order_by(_active_run_priority(), Run.created_at.asc(), Run.id.asc())
                .first()
            )
            if row is None:
                return None
            thread, active_run = row
            return self._summary_record(thread, active_run)
        finally:
            db.close()

    def list_thread_summaries(self, username: str) -> list[ThreadSummaryRecord]:
        db = self._session_factory()
        try:
            user = self._user(db, username)
            if user is None:
                return []
            rows = (
                db.query(Thread, Run)
                .outerjoin(
                    Run,
                    and_(
                        Run.thread_ref_id == Thread.id,
                        Run.status.notin_(tuple(TERMINAL_RUN_STATUSES)),
                    ),
                )
                .filter(Thread.user_id == user.id)
                .order_by(
                    Thread.updated_at.desc(),
                    _active_run_priority(),
                    Run.created_at.asc(),
                    Run.id.asc(),
                )
                .all()
            )
            summaries: list[ThreadSummaryRecord] = []
            seen: set[int] = set()
            for thread, active_run in rows:
                if thread.id in seen:
                    continue
                seen.add(thread.id)
                summaries.append(self._summary_record(thread, active_run))
            return summaries
        finally:
            db.close()

    def delete_thread(self, username: str, thread_id: str) -> bool:
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                if not user:
                    return False
                thread = (
                    self._thread_query(db, user.id, thread_id).with_for_update().first()
                )
                if not thread:
                    return False
                active_run = (
                    db.query(Run.id, Run.status)
                    .filter(
                        Run.thread_ref_id == thread.id,
                        Run.status.notin_(tuple(TERMINAL_RUN_STATUSES)),
                    )
                    .first()
                )
                if active_run:
                    raise AppError(
                        ErrorCode.RUN_ACTIVE,
                        "Thread 仍有活跃 Run，请先取消或等待运行结束",
                        status_code=409,
                        safe_details={
                            "active_run_id": active_run.id,
                            "active_run_status": active_run.status,
                        },
                    )
                db.delete(thread)
                return True
        finally:
            db.close()


thread_repository = ThreadRepository()
