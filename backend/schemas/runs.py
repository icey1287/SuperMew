from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadCreateRequest(RunSchema):
    thread_id: str | None = Field(default=None, min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=160)


class ThreadResponse(RunSchema):
    thread_id: str
    title: str
    status: str
    version: int
    message_count: int
    created_at: str
    updated_at: str


class RunCreateRequest(RunSchema):
    message: str = Field(min_length=1, max_length=100000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_thread_version: int | None = Field(default=None, ge=0)
    multitask_strategy: Literal["reject", "enqueue", "cancel_previous"] | None = None
    on_disconnect: Literal["cancel", "continue"] | None = None


class RunErrorResponse(RunSchema):
    code: str
    message: str
    retryable: bool
    category: str | None = None
    stage: str | None = None
    provider: str | None = None
    retry_after: float | None = Field(default=None, ge=0)


class RunResponse(RunSchema):
    id: str
    thread_id: str
    status: str
    idempotency_key: str
    request_hash: str
    multitask_strategy: str
    fencing_token: int
    user_message_id: int
    assistant_message_id: int
    supersedes_run_id: str | None = None
    model_name: str
    on_disconnect: str
    owner_worker_id: str | None = None
    lease_expires_at: str | None = None
    deadline_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    input_tokens: int
    output_tokens: int
    cost: str
    created_at: str
    updated_at: str
    error: RunErrorResponse | None = None


class RunCreateResponse(RunSchema):
    run: RunResponse
    created: bool
    thread_version: int


class RunResumeRequest(RunSchema):
    hitl_token: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=100000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class RunResumeResponse(RunSchema):
    run: RunResponse
    checkpoint_id: str
    created: bool
