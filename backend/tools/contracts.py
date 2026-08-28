from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from backend.tools.generated.tool_result_v1 import (
    TOOL_RESULT_V1_SCHEMA,
    ToolArtifactV1,
    ToolResultV1,
)


ToolArtifactInput = ToolArtifactV1 | Mapping[str, JsonValue]


def _validate_artifacts(
    artifacts: Sequence[ToolArtifactInput],
) -> list[ToolArtifactV1]:
    return [
        artifact
        if isinstance(artifact, ToolArtifactV1)
        else ToolArtifactV1.model_validate(dict(artifact))
        for artifact in artifacts
    ]


def new_tool_success(
    *,
    data: JsonValue = None,
    duration_ms: int = 0,
    artifacts: Sequence[ToolArtifactInput] = (),
    observability_metadata: Mapping[str, JsonValue] | None = None,
) -> ToolResultV1:
    return ToolResultV1(
        schema_version=1,
        success=True,
        data=data,
        error_code=None,
        retryable=False,
        duration_ms=duration_ms,
        artifacts=_validate_artifacts(artifacts),
        observability_metadata=dict(observability_metadata or {}),
    )


def new_tool_failure(
    *,
    error_code: str,
    retryable: bool,
    duration_ms: int = 0,
    data: JsonValue = None,
    artifacts: Sequence[ToolArtifactInput] = (),
    observability_metadata: Mapping[str, JsonValue] | None = None,
) -> ToolResultV1:
    return ToolResultV1(
        schema_version=1,
        success=False,
        data=data,
        error_code=error_code,
        retryable=retryable,
        duration_ms=duration_ms,
        artifacts=_validate_artifacts(artifacts),
        observability_metadata=dict(observability_metadata or {}),
    )


__all__ = [
    "ToolArtifactV1",
    "ToolResultV1",
    "TOOL_RESULT_V1_SCHEMA",
    "new_tool_failure",
    "new_tool_success",
]
