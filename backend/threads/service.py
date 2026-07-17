from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from backend.threads.repository import (
    MessageRecord,
    ThreadRepository,
    ThreadSummaryRecord,
    thread_repository,
)
from backend.core.errors import AppError, ErrorCode
from backend.threads.contracts import ThreadId, new_thread_id, validate_thread_id


ThreadMessageRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    thread_id: ThreadId
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    version: int
    thread_status: str
    active_run_id: str | None
    active_run_status: str | None


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    id: int
    run_id: str | None
    sequence: int
    status: str
    role: ThreadMessageRole
    content: str
    timestamp: datetime
    rag_trace: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class ThreadMessagePage:
    messages: tuple[ThreadMessage, ...]
    previous_cursor: int | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _message_role(value: str) -> ThreadMessageRole:
    roles: dict[str, ThreadMessageRole] = {
        "human": "user",
        "ai": "assistant",
        "system": "system",
    }
    try:
        return roles[value]
    except KeyError as exc:
        raise RuntimeError(f"unsupported durable Message role: {value}") from exc


def _summary(record: ThreadSummaryRecord) -> ThreadSummary:
    return ThreadSummary(
        thread_id=record.thread_id,
        title=record.title,
        created_at=_utc(record.created_at),
        updated_at=_utc(record.updated_at),
        message_count=record.message_count,
        version=record.version,
        thread_status=record.thread_status,
        active_run_id=record.active_run_id,
        active_run_status=record.active_run_status,
    )


def _message(record: MessageRecord) -> ThreadMessage:
    return ThreadMessage(
        id=record.id,
        run_id=record.run_id,
        sequence=record.sequence,
        status=record.status,
        role=_message_role(record.role),
        content=record.content,
        timestamp=_utc(record.timestamp),
        rag_trace=record.rag_trace,
    )


class ThreadService:
    """Thread application Module used by the canonical HTTP Adapter."""

    def __init__(
        self,
        repository: ThreadRepository = thread_repository,
    ) -> None:
        self.repository = repository

    def create_thread(
        self,
        *,
        username: str,
        thread_id: ThreadId | None = None,
        title: str | None = None,
    ) -> ThreadSummary:
        resolved_id = validate_thread_id(thread_id or new_thread_id())
        resolved_title = " ".join((title or "").split()) or None
        if resolved_title is not None and len(resolved_title) > 160:
            raise ValueError("title 不能超过 160 个字符")
        self.repository.create_thread(
            username=username,
            thread_id=resolved_id,
            title=resolved_title,
        )
        created = self.repository.get_thread_summary(username, resolved_id)
        if created is None:
            raise RuntimeError("created Thread could not be reloaded")
        return _summary(created)

    def list_threads(self, *, username: str) -> list[ThreadSummary]:
        return [
            _summary(record)
            for record in self.repository.list_thread_summaries(username)
        ]

    def recent_messages(
        self,
        *,
        username: str,
        thread_id: str,
        before: int | None = None,
        limit: int = 200,
    ) -> ThreadMessagePage:
        resolved_id = validate_thread_id(thread_id)
        if before is not None and before < 1:
            raise ValueError("before 必须是正整数 sequence")
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        rows = self.repository.list_messages_before(
            username,
            resolved_id,
            before=before,
            limit=limit + 1,
        )
        if rows is None:
            raise AppError(ErrorCode.NOT_FOUND, "Thread 不存在", status_code=404)
        has_previous = len(rows) > limit
        selected = rows[:limit]
        messages = tuple(_message(record) for record in reversed(selected))
        return ThreadMessagePage(
            messages=messages,
            previous_cursor=(messages[0].sequence if has_previous else None),
        )

    def delete_thread(self, *, username: str, thread_id: str) -> bool:
        return self.repository.delete_thread(
            username,
            validate_thread_id(thread_id),
        )


thread_service = ThreadService()


__all__ = [
    "ThreadMessage",
    "ThreadMessagePage",
    "ThreadMessageRole",
    "ThreadService",
    "ThreadSummary",
    "thread_service",
]
