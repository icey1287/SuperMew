from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelRole(StrEnum):
    ANSWER = "answer"
    FAST = "fast"
    GRADER = "grader"
    EVALUATOR = "evaluator"


class ModelRoleRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supports_stream: bool = False
    supports_structured_output: bool = False
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


MODEL_ROLE_REQUIREMENTS = {
    ModelRole.ANSWER: ModelRoleRequirement(
        supports_stream=True,
        temperature=0.3,
    ),
    ModelRole.FAST: ModelRoleRequirement(
        supports_structured_output=True,
        temperature=0.2,
    ),
    ModelRole.GRADER: ModelRoleRequirement(
        supports_structured_output=True,
        temperature=0.0,
    ),
    ModelRole.EVALUATOR: ModelRoleRequirement(
        supports_structured_output=True,
        temperature=0.0,
    ),
}


class ModelProfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^model_[a-f0-9]{32}$")
    display_name: str = Field(min_length=1, max_length=120)
    provider: Literal["openai"] = "openai"
    model_name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=512)
    timeout_seconds: float = Field(gt=0, le=600)
    supports_stream: bool
    supports_structured_output: bool
    enabled: bool
    source: Literal["environment", "user"]
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ModelRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    profile_version: int = Field(ge=1)
    display_name: str
    provider: Literal["openai"] = "openai"
    model_name: str
    base_url: str = ""
    timeout_seconds: float = Field(gt=0, le=600)
    supports_stream: bool
    supports_structured_output: bool

    @classmethod
    def from_profile(cls, profile: ModelProfileRecord) -> ModelRuntimeSpec:
        return cls(
            profile_id=profile.id,
            profile_version=profile.version,
            display_name=profile.display_name,
            provider=profile.provider,
            model_name=profile.model_name,
            base_url=profile.base_url,
            timeout_seconds=profile.timeout_seconds,
            supports_stream=profile.supports_stream,
            supports_structured_output=profile.supports_structured_output,
        )


class ModelCatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignments: dict[ModelRole, ModelRuntimeSpec]

    @model_validator(mode="after")
    def hash_matches_payload(self) -> ModelCatalogSnapshot:
        expected = model_catalog_hash(self.assignments)
        if self.catalog_hash != expected:
            raise ValueError("model catalog hash does not match assignments")
        return self

    def require(self, role: ModelRole | str) -> ModelRuntimeSpec:
        return self.assignments[ModelRole(role)]


def model_catalog_hash(assignments: dict[ModelRole, ModelRuntimeSpec]) -> str:
    payload = {
        role.value: assignments[role].model_dump(mode="json")
        for role in sorted(assignments, key=lambda value: value.value)
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_model_catalog_snapshot(
    assignments: dict[ModelRole, ModelRuntimeSpec],
) -> ModelCatalogSnapshot:
    return ModelCatalogSnapshot(
        catalog_hash=model_catalog_hash(assignments),
        assignments=assignments,
    )


__all__ = [
    "MODEL_ROLE_REQUIREMENTS",
    "ModelCatalogSnapshot",
    "ModelProfileRecord",
    "ModelRole",
    "ModelRoleRequirement",
    "ModelRuntimeSpec",
    "build_model_catalog_snapshot",
    "model_catalog_hash",
]
