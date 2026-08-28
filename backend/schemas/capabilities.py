from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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


class ManagedSkillResponse(CapabilitySchema):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    version: str = Field(min_length=5, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=262_144)
    allowed_tools: tuple[str, ...]
    required_roles: tuple[str, ...]
    required_secrets: tuple[str, ...]
    enabled: bool
    source: Literal["builtin", "custom"]
    created_at: datetime
    updated_at: datetime


class ManagedSkillCreateRequest(CapabilitySchema):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=262_144)
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=128)
    required_roles: tuple[str, ...] = Field(default=(), max_length=32)
    required_secrets: tuple[str, ...] = Field(default=(), max_length=32)
    enabled: bool = True


class ManagedSkillUpdateRequest(CapabilitySchema):
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=262_144)
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=128)
    required_roles: tuple[str, ...] = Field(default=(), max_length=32)
    required_secrets: tuple[str, ...] = Field(default=(), max_length=32)
    enabled: bool = True


class ManagedHttpToolResponse(CapabilitySchema):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$", max_length=128)
    version: str = Field(min_length=5, max_length=64)
    description: str = Field(min_length=1, max_length=1_000)
    group: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2_048)
    method: Literal["GET", "POST"]
    input_schema: dict[str, Any]
    static_headers: dict[str, str]
    secret_headers: dict[str, str]
    required_roles: tuple[str, ...]
    requires_approval: bool
    idempotent: bool
    timeout_seconds: float
    max_response_bytes: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ManagedHttpToolCreateRequest(CapabilitySchema):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$", max_length=128)
    description: str = Field(min_length=1, max_length=1_000)
    group: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$", max_length=128)
    endpoint: str = Field(min_length=1, max_length=2_048)
    method: Literal["GET", "POST"] = "POST"
    input_schema: dict[str, Any]
    static_headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    secret_headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    required_roles: tuple[str, ...] = Field(default=(), max_length=32)
    requires_approval: bool = False
    idempotent: bool = True
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_response_bytes: int = Field(default=262_144, ge=1_024, le=8_388_608)
    enabled: bool = True


class ManagedHttpToolUpdateRequest(CapabilitySchema):
    description: str = Field(min_length=1, max_length=1_000)
    group: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$", max_length=128)
    endpoint: str = Field(min_length=1, max_length=2_048)
    method: Literal["GET", "POST"] = "POST"
    input_schema: dict[str, Any]
    static_headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    secret_headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    required_roles: tuple[str, ...] = Field(default=(), max_length=32)
    requires_approval: bool = False
    idempotent: bool = True
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_response_bytes: int = Field(default=262_144, ge=1_024, le=8_388_608)
    enabled: bool = True


class SqlAssistantConfigResponse(CapabilitySchema):
    enabled: bool
    dsn_secret_name: str
    dsn_configured: bool
    expected_role: str
    allowed_schemas: tuple[str, ...]
    allowed_tables: tuple[str, ...]
    sensitive_columns: tuple[str, ...]
    statement_timeout_seconds: float
    max_rows: int
    max_result_bytes: int
    max_estimated_cost: float
    max_estimated_rows: int
    max_estimated_bytes: int
    catalog_cache_ttl_seconds: float
    updated_at: datetime


class SqlAssistantConfigUpdateRequest(CapabilitySchema):
    enabled: bool
    dsn_secret_name: str = Field(
        default="SQL_ASSISTANT_DSN",
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
        max_length=128,
    )
    expected_role: str = Field(default="", max_length=63)
    allowed_schemas: tuple[str, ...] = Field(default=(), max_length=512)
    allowed_tables: tuple[str, ...] = Field(default=(), max_length=512)
    sensitive_columns: tuple[str, ...] = Field(default=(), max_length=512)
    statement_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_rows: int = Field(default=200, ge=1, le=10_000)
    max_result_bytes: int = Field(default=262_144, ge=1_024, le=16_777_216)
    max_estimated_cost: float = Field(default=100_000.0, gt=0, le=1_000_000_000)
    max_estimated_rows: int = Field(default=100_000, ge=1, le=1_000_000_000)
    max_estimated_bytes: int = Field(default=8_388_608, ge=1_024, le=1_073_741_824)
    catalog_cache_ttl_seconds: float = Field(default=300.0, ge=1, le=3_600)


class WebResearchConfigResponse(CapabilitySchema):
    enabled: bool
    provider: Literal["tavily-keyless"]
    api_key_required: Literal[False]


class WebResearchConfigUpdateRequest(CapabilitySchema):
    enabled: bool


class BuiltinToolAdminResponse(CapabilitySchema):
    name: str
    description: str
    group: str
    version: str
    required_roles: tuple[str, ...]
    requires_approval: bool
    network_policy: str
    resource_scope: str


class CapabilityControlPlaneResponse(CapabilitySchema):
    schema_version: Literal[1] = 1
    web_research: WebResearchConfigResponse
    sql_assistant: SqlAssistantConfigResponse
    skills: tuple[ManagedSkillResponse, ...]
    custom_tools: tuple[ManagedHttpToolResponse, ...]
    builtin_tools: tuple[BuiltinToolAdminResponse, ...]


class CapabilityDeleteResponse(CapabilitySchema):
    name: str
    deleted: bool = True


__all__ = [
    "AvailabilityReason",
    "CapabilityResponse",
    "CapabilitySkillResponse",
    "CapabilityToolResponse",
    "CapabilityControlPlaneResponse",
    "CapabilityDeleteResponse",
    "ManagedHttpToolCreateRequest",
    "ManagedHttpToolUpdateRequest",
    "ManagedSkillCreateRequest",
    "ManagedSkillUpdateRequest",
    "SqlAssistantConfigUpdateRequest",
    "WebResearchConfigUpdateRequest",
]
