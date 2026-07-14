# Generated from contracts/run_event_v1.json. Do not edit by hand.
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_WAITING_INPUT = "run.waiting_input"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    PLANNER_STARTED = "planner.started"
    PLANNER_COMPLETED = "planner.completed"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_DENIED = "tool.denied"
    RETRIEVAL_STARTED = "retrieval.started"
    RETRIEVAL_CANDIDATES = "retrieval.candidates"
    RETRIEVAL_RERANK_COMPLETED = "retrieval.rerank_completed"
    RETRIEVAL_COMPLETED = "retrieval.completed"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    HITL_REQUIRED = "hitl.required"
    HITL_RESUMED = "hitl.resumed"
    USAGE_UPDATED = "usage.updated"
    ARTIFACT_CREATED = "artifact.created"
    WARNING_CREATED = "warning.created"


class RunEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]+$", max_length=80)
    sequence: int = Field(ge=1)
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]+$", max_length=64)
    thread_id: str = Field(min_length=1, max_length=120)
    type: RunEventType
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include timezone")
        return value
