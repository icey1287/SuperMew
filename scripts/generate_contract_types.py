from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_EVENT_SCHEMA_PATH = ROOT / "contracts" / "run_event_v1.json"
RUN_EVENT_PYTHON_PATH = ROOT / "backend" / "events" / "generated" / "run_event_v1.py"
RUN_EVENT_TYPESCRIPT_PATH = (
    ROOT / "frontend" / "src" / "types" / "generated" / "run-event-v1.ts"
)
TOOL_RESULT_SCHEMA_PATH = ROOT / "contracts" / "tool_result_v1.json"
TOOL_RESULT_PYTHON_PATH = ROOT / "backend" / "tools" / "generated" / "tool_result_v1.py"
TOOL_RESULT_TYPESCRIPT_PATH = (
    ROOT / "frontend" / "src" / "types" / "generated" / "tool-result-v1.ts"
)


@dataclass(frozen=True, slots=True)
class GeneratedOutput:
    path: Path
    renderer: Callable[[dict], str]


@dataclass(frozen=True, slots=True)
class ContractGeneration:
    schema_path: Path
    outputs: tuple[GeneratedOutput, ...]


def _load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _enum_member(value: str) -> str:
    return value.upper().replace(".", "_").replace("-", "_")


def render_python(schema: dict) -> str:
    event_types = schema["properties"]["type"]["enum"]
    members = "\n".join(
        f'    {_enum_member(value)} = "{value}"' for value in event_types
    )
    return f"""# Generated from contracts/run_event_v1.json. Do not edit by hand.
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunEventType(StrEnum):
{members}


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
"""


def render_typescript(schema: dict) -> str:
    event_types = schema["properties"]["type"]["enum"]
    union = "\n".join(f"  | '{value}'" for value in event_types)
    return f"""// Generated from contracts/run_event_v1.json. Do not edit by hand.
export type RunEventType =
{union};

export interface RunEventV1<TData extends Record<string, unknown> = Record<string, unknown>> {{
  schema_version: 1;
  event_id: string;
  sequence: number;
  run_id: string;
  thread_id: string;
  type: RunEventType;
  timestamp: string;
  data: TData;
}}
"""


def _tool_result_contract(
    schema: dict,
) -> tuple[dict[str, dict], dict[str, dict]]:
    expected_result = {
        "schema_version",
        "success",
        "data",
        "error_code",
        "retryable",
        "duration_ms",
        "artifacts",
        "observability_metadata",
    }
    expected_artifact = {
        "artifact_id",
        "name",
        "media_type",
        "uri",
        "size_bytes",
        "sha256",
        "metadata",
    }
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise ValueError("tool_result_v1 root must be a closed object")
    result = schema.get("properties") or {}
    artifact_schema = (schema.get("$defs") or {}).get("toolArtifact") or {}
    artifact = artifact_schema.get("properties") or {}
    if set(result) != expected_result:
        raise ValueError("tool_result_v1 properties changed; update the generator")
    if set(schema.get("required") or ()) != expected_result:
        raise ValueError("tool_result_v1 fields must all be required")
    if (
        artifact_schema.get("type") != "object"
        or artifact_schema.get("additionalProperties") is not False
        or set(artifact) != expected_artifact
    ):
        raise ValueError("toolArtifact properties changed; update the generator")
    if set(artifact_schema.get("required") or ()) != {
        "artifact_id",
        "name",
        "media_type",
    }:
        raise ValueError("toolArtifact required fields changed; update the generator")
    if result["artifacts"] != {
        "type": "array",
        "items": {"$ref": "#/$defs/toolArtifact"},
    }:
        raise ValueError("tool_result_v1 artifacts contract changed")
    expected_fixed_result_fields = {
        "schema_version": {"type": "integer", "const": 1},
        "success": {"type": "boolean"},
        "data": {},
        "retryable": {"type": "boolean"},
        "observability_metadata": {
            "type": "object",
            "additionalProperties": True,
        },
    }
    for field_name, expected in expected_fixed_result_fields.items():
        if result[field_name] != expected:
            raise ValueError(
                f"tool_result_v1 {field_name} contract changed; update the generator"
            )
    constrained_result_shapes = {
        "error_code": {"type", "pattern", "maxLength"},
        "duration_ms": {"type", "minimum"},
    }
    for field_name, expected_keys in constrained_result_shapes.items():
        if set(result[field_name]) != expected_keys:
            raise ValueError(
                f"tool_result_v1 {field_name} shape changed; update the generator"
            )
    if set(result["error_code"]["type"]) != {"string", "null"}:
        raise ValueError("tool_result_v1 error_code must be nullable text")
    if result["duration_ms"]["type"] != "integer":
        raise ValueError("tool_result_v1 duration_ms must be an integer")

    constrained_artifact_shapes = {
        "artifact_id": {"type", "pattern", "maxLength"},
        "name": {"type", "minLength", "maxLength"},
        "media_type": {"type", "pattern", "maxLength"},
        "uri": {"type", "pattern", "maxLength"},
        "size_bytes": {"type", "minimum"},
        "sha256": {"type", "pattern"},
    }
    for field_name, expected_keys in constrained_artifact_shapes.items():
        if set(artifact[field_name]) != expected_keys:
            raise ValueError(
                f"toolArtifact {field_name} shape changed; update the generator"
            )
    for field_name in ("artifact_id", "name", "media_type"):
        if artifact[field_name]["type"] != "string":
            raise ValueError(f"toolArtifact {field_name} must be text")
    if set(artifact["uri"]["type"]) != {"string", "null"}:
        raise ValueError("toolArtifact uri must be nullable text")
    if set(artifact["size_bytes"]["type"]) != {"integer", "null"}:
        raise ValueError("toolArtifact size_bytes must be a nullable integer")
    if set(artifact["sha256"]["type"]) != {"string", "null"}:
        raise ValueError("toolArtifact sha256 must be nullable text")
    if artifact["metadata"] != {
        "type": "object",
        "additionalProperties": True,
    }:
        raise ValueError("toolArtifact metadata contract changed")

    expected_outcome_invariant = [
        {
            "if": {
                "properties": {"success": {"const": True}},
                "required": ["success"],
            },
            "then": {
                "properties": {
                    "error_code": {"const": None},
                    "retryable": {"const": False},
                }
            },
            "else": {"properties": {"error_code": {"type": "string"}}},
        }
    ]
    if schema.get("allOf") != expected_outcome_invariant:
        raise ValueError("tool_result_v1 outcome invariant changed")
    return result, artifact


def render_tool_result_python(schema: dict) -> str:
    result, artifact = _tool_result_contract(schema)
    schema_version = result["schema_version"]["const"]
    artifact_id = artifact["artifact_id"]
    artifact_name = artifact["name"]
    media_type = artifact["media_type"]
    uri = artifact["uri"]
    size_bytes = artifact["size_bytes"]
    sha256 = artifact["sha256"]
    error_code = result["error_code"]
    duration_ms = result["duration_ms"]
    template = '''# Generated from contracts/tool_result_v1.json. Do not edit by hand.
from __future__ import annotations

import json
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


TOOL_RESULT_V1_SCHEMA: dict[str, object] = json.loads(
    r"""__SCHEMA_JSON__"""
)


class ToolArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    artifact_id: str = Field(
        pattern=__ARTIFACT_ID_PATTERN__,
        max_length=__ARTIFACT_ID_MAX_LENGTH__,
    )
    name: str = Field(
        min_length=__ARTIFACT_NAME_MIN_LENGTH__,
        max_length=__ARTIFACT_NAME_MAX_LENGTH__,
    )
    media_type: str = Field(
        pattern=__MEDIA_TYPE_PATTERN__,
        max_length=__MEDIA_TYPE_MAX_LENGTH__,
    )
    uri: str | None = Field(
        default=None,
        pattern=__URI_PATTERN__,
        max_length=__URI_MAX_LENGTH__,
    )
    size_bytes: int | None = Field(default=None, ge=__SIZE_BYTES_MINIMUM__)
    sha256: str | None = Field(default=None, pattern=__SHA256_PATTERN__)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[__SCHEMA_VERSION__]
    success: bool
    data: JsonValue
    error_code: str | None = Field(
        pattern=__ERROR_CODE_PATTERN__,
        max_length=__ERROR_CODE_MAX_LENGTH__,
    )
    retryable: bool
    duration_ms: int = Field(ge=__DURATION_MINIMUM__)
    artifacts: list[ToolArtifactV1]
    observability_metadata: dict[str, JsonValue]

    @model_validator(mode="after")
    def outcome_fields_must_be_consistent(self) -> ToolResultV1:
        if self.success:
            if self.error_code is not None:
                raise ValueError("successful tool result cannot have error_code")
            if self.retryable:
                raise ValueError("successful tool result cannot be retryable")
        elif self.error_code is None:
            raise ValueError("failed tool result must have error_code")
        return self
'''
    replacements = {
        "__ARTIFACT_ID_PATTERN__": json.dumps(artifact_id["pattern"]),
        "__ARTIFACT_ID_MAX_LENGTH__": str(artifact_id["maxLength"]),
        "__ARTIFACT_NAME_MIN_LENGTH__": str(artifact_name["minLength"]),
        "__ARTIFACT_NAME_MAX_LENGTH__": str(artifact_name["maxLength"]),
        "__MEDIA_TYPE_PATTERN__": json.dumps(media_type["pattern"]),
        "__MEDIA_TYPE_MAX_LENGTH__": str(media_type["maxLength"]),
        "__URI_PATTERN__": json.dumps(uri["pattern"]),
        "__URI_MAX_LENGTH__": str(uri["maxLength"]),
        "__SIZE_BYTES_MINIMUM__": str(size_bytes["minimum"]),
        "__SHA256_PATTERN__": json.dumps(sha256["pattern"]),
        "__SCHEMA_VERSION__": str(schema_version),
        "__ERROR_CODE_PATTERN__": json.dumps(error_code["pattern"]),
        "__ERROR_CODE_MAX_LENGTH__": str(error_code["maxLength"]),
        "__DURATION_MINIMUM__": str(duration_ms["minimum"]),
        "__SCHEMA_JSON__": json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def render_tool_result_typescript(schema: dict) -> str:
    result, _artifact = _tool_result_contract(schema)
    artifact_required = set(schema["$defs"]["toolArtifact"]["required"])
    template = """// Generated from contracts/tool_result_v1.json. Do not edit by hand.
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface ToolArtifactV1 {
  artifact_id: string;
  name: string;
  media_type: string;
  uri__URI_OPTIONAL__: string | null;
  size_bytes__SIZE_BYTES_OPTIONAL__: number | null;
  sha256__SHA256_OPTIONAL__: string | null;
  metadata__METADATA_OPTIONAL__: Record<string, JsonValue>;
}

interface ToolResultBaseV1 {
  schema_version: __SCHEMA_VERSION__;
  data: JsonValue;
  duration_ms: number;
  artifacts: ToolArtifactV1[];
  observability_metadata: Record<string, JsonValue>;
}

export interface ToolSuccessResultV1 extends ToolResultBaseV1 {
  success: true;
  error_code: null;
  retryable: false;
}

export interface ToolFailureResultV1 extends ToolResultBaseV1 {
  success: false;
  error_code: string;
  retryable: boolean;
}

export type ToolResultV1 = ToolSuccessResultV1 | ToolFailureResultV1;
"""
    replacements = {
        "__URI_OPTIONAL__": "" if "uri" in artifact_required else "?",
        "__SIZE_BYTES_OPTIONAL__": ("" if "size_bytes" in artifact_required else "?"),
        "__SHA256_OPTIONAL__": "" if "sha256" in artifact_required else "?",
        "__METADATA_OPTIONAL__": "" if "metadata" in artifact_required else "?",
        "__SCHEMA_VERSION__": str(result["schema_version"]["const"]),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


CONTRACT_GENERATIONS = (
    ContractGeneration(
        schema_path=RUN_EVENT_SCHEMA_PATH,
        outputs=(
            GeneratedOutput(RUN_EVENT_PYTHON_PATH, render_python),
            GeneratedOutput(RUN_EVENT_TYPESCRIPT_PATH, render_typescript),
        ),
    ),
    ContractGeneration(
        schema_path=TOOL_RESULT_SCHEMA_PATH,
        outputs=(
            GeneratedOutput(TOOL_RESULT_PYTHON_PATH, render_tool_result_python),
            GeneratedOutput(TOOL_RESULT_TYPESCRIPT_PATH, render_tool_result_typescript),
        ),
    ),
)


def _sync(path: Path, content: str, *, check: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return True
    if check:
        print(f"generated contract is stale: {path.relative_to(ROOT)}", file=sys.stderr)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    results = []
    for generation in CONTRACT_GENERATIONS:
        schema = _load_schema(generation.schema_path)
        results.extend(
            _sync(output.path, output.renderer(schema), check=args.check)
            for output in generation.outputs
        )
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
