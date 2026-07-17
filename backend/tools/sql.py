"""Request-owned Tool Adapters for the global read-only SQL runtime."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success


class SqlAssistantRuntime(Protocol):
    """Small Interface consumed by Tool Adapters at the runtime seam."""

    def describe_schema(self, tables: tuple[str, ...]) -> object: ...

    def query(
        self,
        sql: str,
        *,
        deadline_at: float | None,
        cancellation_probe,
    ) -> object: ...


SQL_SCHEMA_METADATA_KEYS = frozenset(
    {"schema_count", "table_count", "column_count", "catalog_cache_hit"}
)
SQL_QUERY_METADATA_KEYS = frozenset(
    {
        "query_fingerprint",
        "row_count",
        "column_count",
        "result_bytes",
        "estimated_cost",
        "masked_column_count",
        "limit_applied",
    }
)


def get_sql_assistant_runtime() -> SqlAssistantRuntime:
    """Resolve the process-wide lazy runtime without capturing it in a Run."""

    from backend.sql_assistant.runtime import (
        get_sql_assistant_runtime as resolve_runtime,
    )

    return resolve_runtime()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("SQL runtime returned a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("SQL runtime returned a non-finite decimal")
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("SQL runtime mappings must use string keys")
            converted[key] = _json_value(item)
        return converted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"SQL runtime returned non-JSON value: {type(value).__name__}")


def _tool_result(
    value: object,
    *,
    metadata_keys: frozenset[str],
) -> ToolResultV1:
    if isinstance(value, ToolResultV1):
        return value

    payload = _json_value(value)
    metadata: Mapping[str, Any] = {}
    if isinstance(payload, dict):
        raw_metadata = payload.pop("observability_metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise TypeError("SQL runtime observability metadata must be a mapping")
        metadata = raw_metadata
    return new_tool_success(
        data=payload,
        observability_metadata={
            key: _json_value(item)
            for key, item in metadata.items()
            if key in metadata_keys
        },
    )


def _sql_failure(error: Exception) -> ToolResultV1 | None:
    try:
        from backend.sql_assistant.postgres import SqlAssistantError
    except ImportError:
        return None
    if not isinstance(error, SqlAssistantError):
        return None
    return new_tool_failure(
        error_code=error.code,
        retryable=error.retryable,
    )


def make_sql_schema(_ctx: RunRequestContext) -> BaseTool:
    """Build a request-owned schema Adapter over the shared SQL runtime."""

    @tool("sql_schema")
    def sql_schema(tables: list[str] | None = None) -> ToolResultV1:
        """Describe authorized SQL tables and columns without exposing other schema."""

        try:
            normalized_tables = tuple(
                dict.fromkeys(table.casefold() for table in (tables or ()))
            )
            result = get_sql_assistant_runtime().describe_schema(normalized_tables)
        except Exception as exc:
            failure = _sql_failure(exc)
            if failure is None:
                raise
            return failure
        return _tool_result(result, metadata_keys=SQL_SCHEMA_METADATA_KEYS)

    return sql_schema


def make_sql_query(ctx: RunRequestContext) -> BaseTool:
    """Build a request-owned query Adapter with Run deadline and cancellation."""

    @tool("sql_query")
    def sql_query(sql: str) -> ToolResultV1:
        """Run one policy-checked, bounded, read-only PostgreSQL query."""

        deadline_at, cancellation_probe = ctx.provider_runtime()
        try:
            result = get_sql_assistant_runtime().query(
                sql,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
        except Exception as exc:
            failure = _sql_failure(exc)
            if failure is None:
                raise
            return failure
        return _tool_result(result, metadata_keys=SQL_QUERY_METADATA_KEYS)

    return sql_query


__all__ = [
    "SqlAssistantRuntime",
    "SQL_QUERY_METADATA_KEYS",
    "SQL_SCHEMA_METADATA_KEYS",
    "get_sql_assistant_runtime",
    "make_sql_query",
    "make_sql_schema",
]
