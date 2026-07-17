from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.schemas.chat import RagTrace
from backend.threads.contracts import ThreadId


class ThreadSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ThreadCreateRequest(ThreadSchema):
    title: str | None = Field(default=None, max_length=160)


class ThreadInfo(ThreadSchema):
    thread_id: ThreadId
    title: str = Field(min_length=1, max_length=160)
    updated_at: datetime
    message_count: int = Field(ge=0)
    version: int = Field(ge=0)
    thread_status: str
    active_run_id: str | None
    active_run_status: str | None

    @field_validator("updated_at")
    @classmethod
    def updated_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Thread timestamps must include timezone")
        return value.astimezone(UTC)


class ThreadResponse(ThreadInfo):
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Thread timestamps must include timezone")
        return value.astimezone(UTC)


class ThreadListResponse(ThreadSchema):
    threads: list[ThreadInfo]


class ThreadMessageInfo(ThreadSchema):
    id: int
    run_id: str | None
    sequence: int = Field(ge=1)
    status: str
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: datetime
    rag_trace: RagTrace | None

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Message timestamp must include timezone")
        return value.astimezone(UTC)


class ThreadMessagesResponse(ThreadSchema):
    messages: list[ThreadMessageInfo]
    previous_cursor: int | None


class ThreadDeleteResponse(ThreadSchema):
    thread_id: ThreadId
    message: str
