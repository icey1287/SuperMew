from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.threads.contracts import ThreadId


class RunSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunCreateRequest(RunSchema):
    message: str = Field(min_length=1, max_length=100000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_thread_version: int | None = Field(default=None, ge=0)
    multitask_strategy: Literal["reject", "enqueue", "cancel_previous"] | None = None
    on_disconnect: Literal["cancel", "continue"] | None = None
    approved_tools: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("approved_tools")
    @classmethod
    def validate_approved_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("approved_tools 不能包含重复项")
        if any(
            not name
            or len(name) > 128
            or not name[0].islower()
            or any(
                not (character.islower() or character.isdigit() or character in "_.-")
                for character in name
            )
            for name in value
        ):
            raise ValueError("approved_tools 包含非法工具名称")
        return value


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
    thread_id: ThreadId
    status: str
    idempotency_key: str
    request_hash: str
    multitask_strategy: str
    fencing_token: int
    user_message_id: int
    assistant_message_id: int
    supersedes_run_id: str | None = None
    model_name: str
    model_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    on_disconnect: str
    owner_worker_id: str | None = None
    lease_expires_at: str | None = None
    deadline_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    skill_name: str | None = None
    skill_version: str | None = None
    skill_content_hash: str | None = None
    skill_activation_source: str | None = None
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
