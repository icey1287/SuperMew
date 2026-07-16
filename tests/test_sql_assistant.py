from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import SecretStr

from backend.core.settings import SqlAssistantSettings
from backend.sql_assistant.contracts import (
    SqlCatalog,
    SqlCatalogColumn,
    SqlCatalogRelation,
)
from backend.sql_assistant.policy import SqlPolicyCompiler
from backend.sql_assistant.postgres import (
    SqlAssistantError,
    SqlAssistantErrorCode,
    SqlPlanEstimate,
    SqlQueryResult,
    _CancellationState,
    _RelationRecord,
    _parse_plan,
)
from backend.sql_assistant.runtime import SqlAssistantRuntime


def _settings(**overrides: Any) -> SqlAssistantSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "dsn": SecretStr("postgresql://reader:private@db.example/supermew"),
        "expected_role": "sql_reader",
        "allowed_schemas_raw": "public",
        "allowed_tables_raw": "public.accounts",
        "sensitive_columns_raw": "public.accounts.email",
        "max_rows": 2,
        "max_result_bytes": 4096,
        "max_cell_bytes": 128,
        "catalog_cache_ttl_seconds": 10,
        "strict_privilege_check": True,
    }
    values.update(overrides)
    return SqlAssistantSettings(**values)


def _catalog() -> SqlCatalog:
    return SqlCatalog(
        database="analytics",
        revision="catalog-v1",
        relations=(
            SqlCatalogRelation(
                schema="public",
                name="accounts",
                owner="analytics_owner",
                columns=(
                    SqlCatalogColumn(
                        name="id",
                        data_type="bigint",
                        nullable=False,
                    ),
                    SqlCatalogColumn(
                        name="email",
                        data_type="text",
                        nullable=False,
                        sensitive=True,
                    ),
                ),
            ),
        ),
    )


class _FakeAdapter:
    def __init__(self, catalog: SqlCatalog) -> None:
        self.catalog = catalog
        self.start_calls = 0
        self.close_calls = 0
        self.catalog_calls = 0
        self.compiled = None

    def start(self) -> None:
        self.start_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def readiness(self) -> dict[str, Any]:
        return {"ready": self.start_calls > self.close_calls}

    def load_catalog(self, **_kwargs: Any) -> SqlCatalog:
        self.catalog_calls += 1
        return self.catalog

    def execute(self, compiled, **_kwargs: Any) -> SqlQueryResult:
        self.compiled = compiled
        return SqlQueryResult(
            columns=("id", "email"),
            rows=((1, "***"),),
            row_count=1,
            truncated=False,
            result_bytes=90,
            estimate=SqlPlanEstimate(
                total_cost=1.25,
                estimated_rows=1,
                estimated_bytes=40,
            ),
            masked_column_count=1,
            query_fingerprint=compiled.shape_fingerprint,
            limit_applied=compiled.limit_applied,
        )


@dataclass
class _Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def test_runtime_has_small_interface_and_projects_safe_payload() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
    )

    schema = runtime.describe_schema(("accounts",))
    result = runtime.query(
        "SELECT id, email FROM accounts ORDER BY id",
        deadline_at=None,
        cancellation_probe=None,
    )

    assert adapter.start_calls == 1
    assert adapter.catalog_calls == 1
    assert schema == {
        "tables": [
            {
                "schema": "public",
                "name": "accounts",
                "kind": "table",
                "columns": [
                    {
                        "name": "id",
                        "data_type": "bigint",
                        "nullable": False,
                        "sensitive": False,
                    },
                    {
                        "name": "email",
                        "data_type": "text",
                        "nullable": False,
                        "sensitive": True,
                    },
                ],
            }
        ],
        "observability_metadata": {
            "schema_count": 1,
            "table_count": 1,
            "column_count": 2,
            "catalog_cache_hit": True,
        },
    }
    assert result["rows"] == [[1, "***"]]
    assert result["observability_metadata"]["masked_column_count"] == 1
    assert result["observability_metadata"]["limit_applied"] == 3
    assert "SELECT" not in str(result)
    assert "private" not in str(result)
    assert adapter.compiled.masked_ordinals == (1,)


def test_catalog_cache_refreshes_after_ttl() -> None:
    adapter = _FakeAdapter(_catalog())
    clock = _Clock()
    runtime = SqlAssistantRuntime(
        settings=_settings(catalog_cache_ttl_seconds=5),
        adapter_factory=lambda _settings: adapter,
        monotonic=clock,
    )

    first = runtime.describe_schema(())
    clock.now += 6
    second = runtime.describe_schema(())

    assert first["observability_metadata"]["catalog_cache_hit"] is True
    assert second["observability_metadata"]["catalog_cache_hit"] is False
    assert adapter.catalog_calls == 2


def test_audit_fingerprint_redacts_literals_but_preserves_query_shape() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
    )

    first = runtime.query(
        "SELECT id, email FROM accounts WHERE id = 1001",
        deadline_at=None,
        cancellation_probe=None,
    )
    first_statement_fingerprint = adapter.compiled.statement_fingerprint
    second = runtime.query(
        "SELECT id, email FROM accounts WHERE id = 987654",
        deadline_at=None,
        cancellation_probe=None,
    )

    assert (
        first["observability_metadata"]["query_fingerprint"]
        == second["observability_metadata"]["query_fingerprint"]
    )
    assert first_statement_fingerprint != adapter.compiled.statement_fingerprint
    assert "1001" not in str(first["observability_metadata"])
    assert "987654" not in str(second["observability_metadata"])


def test_readiness_exposes_catalog_hash_without_database_identity() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
    )

    before = runtime.readiness()
    runtime.start()
    after = runtime.readiness()

    assert before.enabled is True
    assert before.ready is False
    assert before.catalog_hash is None
    assert after.ready is True
    assert after.catalog_hash == _catalog().fingerprint
    assert "dsn" not in str(after.to_dict()).lower()
    assert "sql_reader" not in str(after.to_dict())
    assert "private" not in str(after.to_dict())


def test_disabled_readiness_is_safe_without_starting_pool() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(enabled=False),
        adapter_factory=lambda _settings: adapter,
    )

    snapshot = runtime.readiness()

    assert snapshot.enabled is False
    assert snapshot.ready is False
    assert snapshot.catalog_hash is None
    assert adapter.start_calls == 0


def test_runtime_rejects_disabled_strict_checks_and_redacts_policy_input() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(strict_privilege_check=False),
        adapter_factory=lambda _settings: adapter,
    )

    with pytest.raises(SqlAssistantError) as captured:
        runtime.query(
            "SELECT 'top-secret' FROM accounts",
            deadline_at=None,
            cancellation_probe=None,
        )

    assert captured.value.code == "SQL_SECURITY_CHECK_FAILED"
    assert "top-secret" not in str(captured.value)
    assert "private" not in str(captured.value)
    assert adapter.start_calls == 0


def test_runtime_maps_policy_failure_without_sql_or_catalog_details() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
    )

    with pytest.raises(SqlAssistantError) as captured:
        runtime.query(
            "DELETE FROM accounts WHERE email = 'secret@example.com'",
            deadline_at=None,
            cancellation_probe=None,
        )

    assert captured.value.code == "SQL_POLICY_DENIED"
    assert "DELETE" not in str(captured.value)
    assert "secret@example.com" not in str(captured.value)


def test_runtime_default_function_policy_uses_sqlglot_canonical_names() -> None:
    adapter = _FakeAdapter(_catalog())
    runtime = SqlAssistantRuntime(
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
    )

    runtime.query(
        "SELECT ceiling(id), now() FROM accounts",
        deadline_at=None,
        cancellation_probe=None,
    )

    assert adapter.compiled is not None
    assert "CEIL" in adapter.compiled.normalized_sql
    assert "CURRENT_TIMESTAMP" in adapter.compiled.normalized_sql


def test_query_result_repr_does_not_expose_rows() -> None:
    result = SqlQueryResult(
        columns=("email",),
        rows=(("private@example.com",),),
        row_count=1,
        truncated=False,
        result_bytes=42,
        estimate=SqlPlanEstimate(
            total_cost=1.0,
            estimated_rows=1,
            estimated_bytes=42,
        ),
        masked_column_count=0,
        query_fingerprint="a" * 64,
        limit_applied=3,
    )

    assert "private@example.com" not in repr(result)


def test_explain_budget_uses_deepest_plan_rows_and_bytes() -> None:
    estimate = _parse_plan(
        [
            {
                "Plan": {
                    "Node Type": "Limit",
                    "Total Cost": 12.0,
                    "Plan Rows": 3,
                    "Plan Width": 16,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Total Cost": 11.0,
                            "Plan Rows": 5000,
                            "Plan Width": 200,
                        }
                    ],
                }
            }
        ]
    )

    assert estimate == SqlPlanEstimate(
        total_cost=12.0,
        estimated_rows=5000,
        estimated_bytes=1_000_000,
    )


class _Column:
    def __init__(self, name: str) -> None:
        self.name = name


class _ServerCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self._offset = 0
        self.description = (_Column("id"), _Column("email"))
        self.fetch_sizes: list[int] = []
        self.itersize = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, _query: str, _params: object) -> None:
        return None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.fetch_sizes.append(size)
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch


class _ResultConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.server_cursor = _ServerCursor(rows)

    def cursor(self, *, name: str):
        assert name.startswith("sql_assistant_")
        return self.server_cursor


def test_incremental_fetch_masks_sensitive_cells_and_detects_truncation() -> None:
    settings = _settings(fetch_size=64)
    compiler = SqlPolicyCompiler(
        SqlAssistantRuntime(
            settings=settings,
            adapter_factory=lambda _settings: _FakeAdapter(_catalog()),
        )._policy
    )
    compiled = compiler.compile(
        "SELECT id, email FROM accounts ORDER BY id",
        _catalog(),
    )
    connection = _ResultConnection(
        [
            (1, "first@example.com"),
            (2, "second@example.com"),
            (3, "third@example.com"),
        ]
    )
    adapter = __import__(
        "backend.sql_assistant.postgres",
        fromlist=["PostgresSqlAssistantAdapter"],
    ).PostgresSqlAssistantAdapter(settings=settings)

    result = adapter._execute_cursor(
        connection,
        compiled,
        estimate=SqlPlanEstimate(1.0, 3, 90),
        state=_CancellationState(),
        deadline_at=None,
        cancellation_probe=None,
    )

    assert result.rows == ((1, "***"), (2, "***"))
    assert result.truncated is True
    assert connection.server_cursor.fetch_sizes == [3]
    assert "first@example.com" not in str(result)


def test_relation_owner_and_select_privilege_are_fail_closed() -> None:
    from backend.sql_assistant.postgres import PostgresSqlAssistantAdapter

    adapter = PostgresSqlAssistantAdapter(settings=_settings())
    owned = _RelationRecord(
        oid=1,
        schema="public",
        name="accounts",
        relkind="r",
        owner="sql_reader",
        can_select=True,
        has_any_column_select=True,
        row_security=True,
        options=(),
    )
    unreadable = _RelationRecord(
        oid=1,
        schema="public",
        name="accounts",
        relkind="r",
        owner="analytics_owner",
        can_select=False,
        has_any_column_select=False,
        row_security=True,
        options=(),
    )
    without_rls = _RelationRecord(
        oid=1,
        schema="public",
        name="accounts",
        relkind="r",
        owner="analytics_owner",
        can_select=True,
        has_any_column_select=True,
        row_security=False,
        options=(),
    )
    materialized_view = _RelationRecord(
        oid=1,
        schema="public",
        name="accounts",
        relkind="m",
        owner="analytics_owner",
        can_select=True,
        has_any_column_select=True,
        row_security=False,
        options=(),
    )

    for record in (owned, unreadable, without_rls, materialized_view):
        with pytest.raises(SqlAssistantError) as captured:
            adapter._validate_relation(record)
        assert captured.value.code == "SQL_SECURITY_CHECK_FAILED"
        assert "accounts" not in str(captured.value)


def test_only_security_invoker_barrier_views_can_cross_rls_seam() -> None:
    from backend.sql_assistant.postgres import PostgresSqlAssistantAdapter

    adapter = PostgresSqlAssistantAdapter(settings=_settings())
    unsafe_view = _RelationRecord(
        oid=1,
        schema="public",
        name="accounts",
        relkind="v",
        owner="analytics_owner",
        can_select=True,
        has_any_column_select=True,
        row_security=False,
        options=("security_invoker=true",),
    )
    safe_view = _RelationRecord(
        oid=1,
        schema="public",
        name="accounts",
        relkind="v",
        owner="analytics_owner",
        can_select=True,
        has_any_column_select=True,
        row_security=False,
        options=("security_invoker=true", "security_barrier=true"),
    )

    with pytest.raises(SqlAssistantError) as captured:
        adapter._validate_relation(unsafe_view)
    assert captured.value.code == "SQL_SECURITY_CHECK_FAILED"
    adapter._validate_relation(safe_view)


@pytest.mark.parametrize(
    ("namespace", "type_kind"),
    [
        ("public", "b"),
        ("pg_catalog", "d"),
        ("pg_catalog", "c"),
        ("analytics", "e"),
    ],
)
def test_catalog_rejects_custom_domain_composite_enum_and_extension_types(
    namespace: str,
    type_kind: str,
) -> None:
    from backend.sql_assistant.postgres import PostgresSqlAssistantAdapter

    with pytest.raises(SqlAssistantError) as captured:
        PostgresSqlAssistantAdapter._validate_column_type(
            namespace=namespace,
            type_kind=type_kind,
        )
    assert captured.value.code == "SQL_SECURITY_CHECK_FAILED"
    assert namespace not in str(captured.value)


def test_catalog_accepts_only_pg_catalog_base_types() -> None:
    from backend.sql_assistant.postgres import PostgresSqlAssistantAdapter

    PostgresSqlAssistantAdapter._validate_column_type(
        namespace="pg_catalog",
        type_kind="b",
    )


class _RelationCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.query = ""

    def execute(self, query: str, _params: object) -> None:
        self.query = query
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def test_strict_privilege_check_rejects_select_outside_table_allowlist() -> None:
    from backend.sql_assistant.postgres import PostgresSqlAssistantAdapter

    adapter = PostgresSqlAssistantAdapter(settings=_settings())
    cursor = _RelationCursor(
        [
            (
                1,
                "public",
                "accounts",
                "r",
                "analytics_owner",
                True,
                True,
                True,
                None,
            ),
            (
                2,
                "finance",
                "payroll",
                "r",
                "analytics_owner",
                False,
                True,
                True,
                None,
            ),
        ]
    )

    with pytest.raises(SqlAssistantError) as captured:
        adapter._load_relation_records(cursor)

    assert captured.value.code == "SQL_SECURITY_CHECK_FAILED"
    assert "payroll" not in str(captured.value)
    assert "namespace.nspname = ANY" not in cursor.query
    assert "namespace.nspname !~ '^pg_'" in cursor.query


def test_public_error_never_contains_sql_dsn_row_or_internal_exception() -> None:
    sensitive_fragments = {
        "SELECT * FROM payroll",
        "postgresql://reader:secret@db/payroll",
        "salary=999999",
        "division by zero at executor.c:42",
    }
    for code in SqlAssistantErrorCode:
        error = SqlAssistantError(code, retryable=True)
        rendered = repr(error)
        assert error.code == code.value
        assert error.retryable is True
        assert not any(fragment in rendered for fragment in sensitive_fragments)
