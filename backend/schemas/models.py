from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.model_control.contracts import (
    ModelProfileRecord,
    ModelRoleRequirement,
)


class ModelSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProfileCreateRequest(ModelSchema):
    display_name: str = Field(min_length=1, max_length=120)
    provider: Literal["openai"] = "openai"
    model_name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=512)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    supports_stream: bool = True
    supports_structured_output: bool = True
    enabled: bool = True


class ModelProfileUpdateRequest(ModelProfileCreateRequest):
    pass


class ModelAssignmentRequest(ModelSchema):
    profile_id: str = Field(pattern=r"^model_[a-f0-9]{32}$")


class ModelDeleteResponse(ModelSchema):
    profile_id: str
    deleted: bool


class ModelControlPlaneResponse(ModelSchema):
    schema_version: Literal[1] = 1
    catalog_hash: str
    api_key_configured: bool
    profiles: tuple[ModelProfileRecord, ...]
    assignments: dict[str, ModelProfileRecord | None]
    requirements: dict[str, ModelRoleRequirement]
