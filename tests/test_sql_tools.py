from __future__ import annotations

from backend.runs.request_context import RunRequestContext
from backend.core.settings import SqlAssistantSettings
from backend.sql_assistant.postgres import SqlAssistantError, SqlAssistantErrorCode
from backend.tools.catalog import (
    build_default_tool_registry,
    configured_secret_names,
)
from backend.tools.contracts import TOOL_RESULT_V1_SCHEMA, ToolResultV1
from backend.tools.registry import ToolAccess, ToolExposure


def _settings(*, enabled: bool = True, dsn: str = "postgresql://reader:x@db/a"):
    return SqlAssistantSettings(
        _env_file=None,
        SQL_ASSISTANT_ENABLED=enabled,
        SQL_ASSISTANT_DSN=dsn,
        SQL_ASSISTANT_EXPECTED_ROLE="analytics_reader",
        SQL_ASSISTANT_ALLOWED_SCHEMAS="analytics",
        SQL_ASSISTANT_ALLOWED_TABLES="analytics.orders,analytics.customers",
        SQL_ASSISTANT_SENSITIVE_COLUMNS="analytics.customers.email",
    )


def _access(*, secrets=frozenset({"SQL_ASSISTANT_DSN"})) -> ToolAccess:
    return ToolAccess(
        roles=frozenset({"admin"}),
        available_secrets=secrets,
        caller_allowed_tools=frozenset({"sql_schema", "sql_query"}),
        approved_tools=frozenset(),
        allowed_network_policies=frozenset({"private-data"}),
    )


def test_catalog_registers_sql_tools_as_admin_only_deferred_adapters():
    settings = _settings()
    registry = build_default_tool_registry(sql_assistant_settings=settings)

    for name in ("sql_schema", "sql_query"):
        descriptor = registry.descriptor(name)
        assert descriptor is not None
        assert descriptor.output_schema == TOOL_RESULT_V1_SCHEMA
        assert descriptor.required_roles == frozenset({"admin"})
        assert descriptor.required_secrets == frozenset({"SQL_ASSISTANT_DSN"})
        assert descriptor.network_policy == "private-data"
        assert registry.exposure(name) is ToolExposure.DEFERRED

    assert registry.descriptor("sql_schema").observability_metadata_keys == frozenset(
        {"schema_count", "table_count", "column_count", "catalog_cache_hit"}
    )
    assert registry.descriptor("sql_query").observability_metadata_keys == frozenset(
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


def test_disabled_or_secretless_sql_assistant_is_not_authorized():
    disabled = _settings(enabled=False)
    registry = build_default_tool_registry(sql_assistant_settings=disabled)

    assert configured_secret_names(
        registry,
        sql_assistant_settings=disabled,
    ).isdisjoint({"SQL_ASSISTANT_DSN"})
    assert registry.describe("sql_query", _access(secrets=frozenset())) is None

    enabled_without_dsn = _settings(enabled=True, dsn="")
    assert configured_secret_names(
        registry,
        sql_assistant_settings=enabled_without_dsn,
    ).isdisjoint({"SQL_ASSISTANT_DSN"})

    enabled = _settings()
    assert "SQL_ASSISTANT_DSN" in configured_secret_names(
        registry,
        sql_assistant_settings=enabled,
    )


def test_sql_query_adapter_passes_run_deadline_and_cancellation():
    calls: list[dict] = []

    class Runtime:
        def query(self, sql, *, deadline_at, cancellation_probe):
            calls.append(
                {
                    "sql": sql,
                    "deadline_at": deadline_at,
                    "cancellation_probe": cancellation_probe,
                }
            )
            return {
                "columns": ["order_count"],
                "rows": [[3]],
                "row_count": 1,
                "observability_metadata": {
                    "query_fingerprint": "sha256:query",
                    "row_count": 1,
                    "column_count": 1,
                    "result_bytes": 32,
                    "estimated_cost": 1.25,
                    "masked_column_count": 0,
                    "limit_applied": True,
                    "raw_sql": "must-not-escape",
                },
            }

    runtime = Runtime()
    def cancelled() -> bool:
        return False

    ctx = RunRequestContext.for_sync(user_id="admin", thread_id="sql-query")
    ctx.configure_provider_runtime(
        deadline_at=1234.5,
        cancellation_probe=cancelled,
    )
    registry = build_default_tool_registry(
        sql_assistant_settings=_settings(),
        sql_runtime=runtime,
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"sql_schema", "sql_query"})

    payload = session.resolve("sql_query").invoke(
        {"sql": "SELECT count(*) AS order_count FROM analytics.orders"}
    )
    result = ToolResultV1.model_validate_json(payload)

    assert result.success is True
    assert result.data["rows"] == [[3]]
    assert result.observability_metadata["query_fingerprint"] == "sha256:query"
    assert "raw_sql" not in result.observability_metadata
    assert calls == [
        {
            "sql": "SELECT count(*) AS order_count FROM analytics.orders",
            "deadline_at": 1234.5,
            "cancellation_probe": cancelled,
        }
    ]
    ctx.close()


def test_sql_schema_adapter_passes_only_requested_qualified_tables():
    calls: list[tuple[str, ...]] = []

    class Runtime:
        def describe_schema(self, tables):
            calls.append(tables)
            return {
                "tables": [
                    {
                        "schema": "analytics",
                        "name": "orders",
                        "kind": "table",
                        "columns": [
                            {
                                "name": "id",
                                "data_type": "bigint",
                                "nullable": False,
                                "sensitive": False,
                            }
                        ],
                    }
                ],
                "observability_metadata": {
                    "schema_count": 1,
                    "table_count": 1,
                    "column_count": 1,
                    "catalog_cache_hit": False,
                    "dsn": "must-not-escape",
                },
            }

    ctx = RunRequestContext.for_sync(user_id="admin", thread_id="sql-schema")
    registry = build_default_tool_registry(
        sql_assistant_settings=_settings(),
        sql_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"sql_schema", "sql_query"})

    payload = session.resolve("sql_schema").invoke({"tables": ["Analytics.Orders"]})
    result = ToolResultV1.model_validate_json(payload)

    assert result.success is True
    assert calls == [("analytics.orders",)]
    assert result.observability_metadata["table_count"] == 1
    assert "dsn" not in result.observability_metadata
    ctx.close()


def test_sql_runtime_exception_is_redacted_by_registry():
    class Runtime:
        def query(self, sql, *, deadline_at, cancellation_probe):
            raise RuntimeError("postgresql://reader:private-password@db/analytics")

    ctx = RunRequestContext.for_sync(user_id="admin", thread_id="sql-failure")
    registry = build_default_tool_registry(
        sql_assistant_settings=_settings(),
        sql_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"sql_query"})

    payload = session.resolve("sql_query").invoke({"sql": "SELECT 1"})
    result = ToolResultV1.model_validate_json(payload)

    assert result.success is False
    assert result.error_code == "TOOL_UNAVAILABLE"
    assert "private-password" not in payload
    ctx.close()


def test_sql_runtime_stable_error_is_preserved_without_internal_message():
    class Runtime:
        def query(self, sql, *, deadline_at, cancellation_probe):
            raise SqlAssistantError(
                SqlAssistantErrorCode.QUERY_TIMEOUT,
                retryable=True,
            )

    ctx = RunRequestContext.for_sync(user_id="admin", thread_id="sql-timeout")
    registry = build_default_tool_registry(
        sql_assistant_settings=_settings(),
        sql_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"sql_query"})

    payload = session.resolve("sql_query").invoke({"sql": "SELECT 1"})
    result = ToolResultV1.model_validate_json(payload)

    assert result.success is False
    assert result.error_code == "SQL_QUERY_TIMEOUT"
    assert result.retryable is True
    assert result.data is None
    assert "SQL 查询超时" not in payload
    ctx.close()
