from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.skills.registry import SkillManifest
from backend.tools.registry import ToolDescriptor


_EMPTY_INPUT_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_HEADER_NAME = r"^[A-Za-z][A-Za-z0-9-]{0,126}$"
_SECRET_NAME = r"^[A-Z][A-Z0-9_]{0,127}$"
_MAX_INPUT_SCHEMA_BYTES = 65_536
_MAX_INPUT_SCHEMA_DEPTH = 32
_MAX_INPUT_SCHEMA_NODES = 4_096


class CapabilityControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManagedSkillRecord(CapabilityControlModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    version: str = Field(min_length=5, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=262_144)
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=128)
    required_roles: tuple[str, ...] = Field(default=(), max_length=32)
    required_secrets: tuple[str, ...] = Field(default=(), max_length=32)
    enabled: bool = True
    source: Literal["builtin", "custom"]
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_manifest_contract(self) -> ManagedSkillRecord:
        SkillManifest(
            schema_version=1,
            name=self.name,
            version=self.version,
            description=self.description,
            allowed_tools=self.allowed_tools,
            required_roles=self.required_roles,
            required_secrets=self.required_secrets,
            entrypoint="SKILL.md",
        )
        return self


class ManagedHttpToolRecord(CapabilityControlModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$", max_length=128)
    version: str = Field(min_length=5, max_length=64)
    description: str = Field(min_length=1, max_length=1_000)
    group: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$", max_length=128)
    endpoint: str = Field(min_length=1, max_length=2_048)
    method: Literal["GET", "POST"]
    input_schema: dict = Field(default_factory=lambda: dict(_EMPTY_INPUT_SCHEMA))
    static_headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    secret_headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    required_roles: tuple[str, ...] = Field(default=(), max_length=32)
    requires_approval: bool = False
    idempotent: bool = True
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_response_bytes: int = Field(default=262_144, ge=1_024, le=8_388_608)
    enabled: bool = True
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_validator("static_headers")
    @classmethod
    def validate_static_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_headers(value, secret_refs=False)

    @field_validator("secret_headers")
    @classmethod
    def validate_secret_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_headers(value, secret_refs=True)

    @model_validator(mode="after")
    def validate_descriptor_contract(self) -> ManagedHttpToolRecord:
        if set(map(str.casefold, self.static_headers)).intersection(
            map(str.casefold, self.secret_headers)
        ):
            raise ValueError("static_headers and secret_headers cannot overlap")
        if self.input_schema.get("type") != "object":
            raise ValueError("input_schema root type must be object")
        properties = self.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("input_schema properties must be an object")
        _validate_custom_input_schema(self.input_schema)
        ToolDescriptor(
            name=self.name,
            description=self.description,
            group=self.group,
            version=self.version,
            input_schema=self.input_schema,
            output_schema={"type": "object"},
            timeout=self.timeout_seconds,
            max_concurrency=4,
            idempotent=self.idempotent,
            required_roles=frozenset(self.required_roles),
            required_secrets=frozenset(self.secret_headers.values()),
            requires_approval=self.requires_approval,
            network_policy="restricted",
            result_size_limit=self.max_response_bytes + 65_536,
            resource_scope="public-web",
        )
        return self


class SqlAssistantConfigRecord(CapabilityControlModel):
    enabled: bool = False
    dsn_secret_name: str = Field(
        default="SQL_ASSISTANT_DSN",
        pattern=_SECRET_NAME,
        max_length=128,
    )
    dsn_configured: bool = False
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
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _validate_headers(
    value: dict[str, str],
    *,
    secret_refs: bool,
) -> dict[str, str]:
    blocked = {
        "accept-encoding",
        "connection",
        "content-type",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "transfer-encoding",
    }
    sensitive = {"authorization", "proxy-authorization", "x-api-key", "api-key"}
    sensitive_markers = (
        "authorization",
        "api-key",
        "apikey",
        "access-token",
        "auth-token",
        "credential",
        "password",
        "secret",
        "signature",
    )
    normalized: dict[str, str] = {}
    seen: set[str] = set()
    import re

    for raw_name, raw_value in value.items():
        name = str(raw_name).strip()
        folded = name.casefold()
        item = str(raw_value).strip()
        if re.fullmatch(_HEADER_NAME, name) is None or folded in blocked or folded in seen:
            raise ValueError("headers contain an invalid or duplicate name")
        if not secret_refs and (
            folded in sensitive or any(marker in folded for marker in sensitive_markers)
        ):
            raise ValueError("sensitive headers must use secret_headers")
        if secret_refs:
            if re.fullmatch(_SECRET_NAME, item) is None:
                raise ValueError("secret_headers values must be environment Secret names")
        elif (
            not item
            or len(item) > 4_096
            or any(marker in item for marker in ("\r", "\n", "\x00"))
            or item.casefold().startswith(("bearer ", "basic "))
        ):
            raise ValueError("static header values must be safe single-line text")
        normalized[name] = item
        seen.add(folded)
    return normalized


def _validate_custom_input_schema(schema: dict) -> None:
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("input_schema must be JSON serializable") from exc
    if len(encoded) > _MAX_INPUT_SCHEMA_BYTES:
        raise ValueError("input_schema exceeds the size limit")

    stack: list[tuple[object, int]] = [(schema, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_INPUT_SCHEMA_NODES:
            raise ValueError("input_schema exceeds the node limit")
        if depth > _MAX_INPUT_SCHEMA_DEPTH:
            raise ValueError("input_schema exceeds the depth limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("input_schema references must stay local")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)


__all__ = [
    "ManagedHttpToolRecord",
    "ManagedSkillRecord",
    "SqlAssistantConfigRecord",
]
