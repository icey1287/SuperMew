from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AvailabilityReason = Literal["permission_required", "not_configured"]


class CapabilitySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CapabilitySkillResponse(CapabilitySchema):
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    activation: str = Field(min_length=2, max_length=65)
    available: bool
    availability_reason: AvailabilityReason | None
    required_roles: tuple[str, ...]
    tool_names: tuple[str, ...]
    approval_tools: tuple[str, ...]
    network_policies: tuple[str, ...]
    resource_scopes: tuple[str, ...]


class CapabilityToolResponse(CapabilitySchema):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1_000)
    group: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    exposure: Literal["resident", "control", "deferred"]
    available: bool
    availability_reason: AvailabilityReason | None
    required_roles: tuple[str, ...]
    requires_approval: bool
    network_policy: str = Field(min_length=1, max_length=64)
    resource_scope: str = Field(min_length=1, max_length=64)
    idempotent: bool


class CapabilityResponse(CapabilitySchema):
    schema_version: Literal[1] = 1
    catalog_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    skills: tuple[CapabilitySkillResponse, ...]
    tools: tuple[CapabilityToolResponse, ...]


__all__ = [
    "AvailabilityReason",
    "CapabilityResponse",
    "CapabilitySkillResponse",
    "CapabilityToolResponse",
]
