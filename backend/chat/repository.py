from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.errors import AppError, ErrorCode
from backend.db.models import ChatMessage, ChatSession, User, utcnow
from backend.infra.database import SessionLocal
from backend.schemas.chat import normalize_rag_trace


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


class ConversationRepository:
    """Thread 与消息 journal 的唯一持久化 interface。"""

    def __init__(self, session_factory: SessionFactory = SessionLocal):
        self._session_factory = session_factory

    @staticmethod
    def _user(db: Session, username: str) -> User | None:
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def _thread_query(db: Session, user_id: int, thread_id: str):
        return db.query(ChatSession).filter(
            ChatSession.user_id == user_id,
            ChatSession.session_id == thread_id,
        )

    def _get_or_create_thread(
        self,
        db: Session,
        user: User,
        thread_id: str,
        *,
        metadata: dict | None = None,
        lock: bool = False,
    ) -> ChatSession:
        query = self._thread_query(db, user.id, thread_id)
        if lock:
            query = query.with_for_update()
        thread = query.first()
        if thread:
            return thread
        thread = ChatSession(
            user_id=user.id,
            session_id=thread_id,
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
    def _record(message: ChatMessage, thread_id: str) -> MessageRecord:
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
    def _assert_version(thread: ChatSession, expected_version: int | None) -> None:
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
                        db.query(ChatMessage)
                        .filter(
                            ChatMessage.session_ref_id == thread.id,
                            ChatMessage.client_message_id == message.client_message_id,
                        )
                        .first()
                    )
                    if existing:
                        return self._record(existing, thread_id)

                now = utcnow()
                row = ChatMessage(
                    session_ref_id=thread.id,
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
                    db.query(ChatMessage)
                    .filter(
                        ChatMessage.id == message_id,
                        ChatMessage.session_ref_id == thread.id,
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
                thread.version += 1
                thread.updated_at = row.updated_at
                db.flush()
                return self._record(row, thread_id)
        finally:
            db.close()

    def sync_legacy_snapshot(
        self,
        username: str,
        thread_id: str,
        messages: Iterable,
        *,
        metadata: dict | None = None,
        extra_message_data: list | None = None,
    ) -> None:
        """旧 ChatService adapter：只追加快照中尚未持久化的尾部。"""
        incoming = list(messages)
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                if not user:
                    return
                thread = self._get_or_create_thread(
                    db,
                    user,
                    thread_id,
                    metadata=metadata,
                    lock=True,
                )
                rows = (
                    db.query(ChatMessage)
                    .filter(ChatMessage.session_ref_id == thread.id)
                    .order_by(ChatMessage.sequence.asc())
                    .all()
                )
                now = utcnow()
                for index in range(len(rows), len(incoming)):
                    item = incoming[index]
                    extra = (
                        extra_message_data[index]
                        if extra_message_data and index < len(extra_message_data)
                        else None
                    ) or {}
                    row = ChatMessage(
                        session_ref_id=thread.id,
                        sequence=thread.last_sequence + 1,
                        message_type=item.type,
                        content=str(item.content),
                        status="completed",
                        timestamp=now,
                        updated_at=now,
                        rag_trace=normalize_rag_trace(extra.get("rag_trace")),
                    )
                    db.add(row)
                    rows.append(row)
                    thread.last_sequence = row.sequence
                    thread.message_count += 1
                    thread.version += 1

                if extra_message_data:
                    for index, row in enumerate(rows[: len(incoming)]):
                        extra = (
                            extra_message_data[index]
                            if index < len(extra_message_data)
                            else None
                        ) or {}
                        if "rag_trace" in extra:
                            row.rag_trace = normalize_rag_trace(extra.get("rag_trace"))
                            row.updated_at = now

                if metadata is not None:
                    thread.metadata_json = {**(thread.metadata_json or {}), **metadata}
                thread.updated_at = now
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "并发追加消息失败，请重试",
                status_code=409,
                retryable=True,
            ) from exc
        finally:
            db.close()

    def list_messages(
        self,
        username: str,
        thread_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> list[MessageRecord]:
        db = self._session_factory()
        try:
            user = self._user(db, username)
            if not user:
                return []
            thread = self._thread_query(db, user.id, thread_id).first()
            if not thread:
                return []
            rows = (
                db.query(ChatMessage)
                .filter(
                    ChatMessage.session_ref_id == thread.id,
                    ChatMessage.sequence > max(after, 0),
                )
                .order_by(ChatMessage.sequence.asc())
                .limit(max(1, min(limit, 500)))
                .all()
            )
            return [self._record(row, thread_id) for row in rows]
        finally:
            db.close()

    def thread_metadata(self, username: str, thread_id: str) -> dict:
        db = self._session_factory()
        try:
            user = self._user(db, username)
            if not user:
                return {}
            thread = self._thread_query(db, user.id, thread_id).first()
            return dict(thread.metadata_json or {}) if thread else {}
        finally:
            db.close()

    def list_threads(self, username: str) -> list[dict]:
        db = self._session_factory()
        try:
            user = self._user(db, username)
            if not user:
                return []
            rows = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id)
                .order_by(ChatSession.updated_at.desc())
                .all()
            )
            return [
                {
                    "session_id": row.session_id,
                    "title": (row.metadata_json or {}).get("title") or row.session_id,
                    "updated_at": row.updated_at.isoformat(),
                    "message_count": row.message_count,
                    "version": row.version,
                    "status": row.status,
                }
                for row in rows
            ]
        finally:
            db.close()

    def delete_thread(self, username: str, thread_id: str) -> bool:
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                if not user:
                    return False
                thread = self._thread_query(db, user.id, thread_id).first()
                if not thread:
                    return False
                db.delete(thread)
                return True
        finally:
            db.close()


repository = ConversationRepository()
