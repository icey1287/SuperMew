from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.rag import RagTrace


class LegacyChatSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(LegacyChatSchema):
    message: str = Field(min_length=1, max_length=100000)
    session_id: str | None = Field(
        default="default_session",
        min_length=1,
        max_length=120,
    )


class ChatResponse(LegacyChatSchema):
    response: str
    rag_trace: RagTrace | None = None


class MessageInfo(LegacyChatSchema):
    type: str
    content: str
    timestamp: datetime
    rag_trace: RagTrace | None = None


class SessionMessagesResponse(LegacyChatSchema):
    messages: list[MessageInfo]


class SessionInfo(LegacyChatSchema):
    session_id: str = Field(min_length=1, max_length=120)
    title: str
    updated_at: datetime
    message_count: int = Field(ge=0)


class SessionListResponse(LegacyChatSchema):
    sessions: list[SessionInfo]


class SessionDeleteResponse(LegacyChatSchema):
    session_id: str = Field(min_length=1, max_length=120)
    message: str


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "MessageInfo",
    "SessionDeleteResponse",
    "SessionInfo",
    "SessionListResponse",
    "SessionMessagesResponse",
]
