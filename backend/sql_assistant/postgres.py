"""Fail-closed PostgreSQL Adapter for the read-only SQL Assistant.

The Adapter owns an independent psycopg3 pool.  Every catalog read and query
runs inside a repeatable-read, read-only transaction with a fixed search path.
Runtime identity and privileges are revalidated in the same snapshot used for
planning and execution so a cached catalog can never become an authorization
decision by itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any

import psycopg
from psycopg import IsolationLevel, sql as pg_sql
from psycopg.errors import LockNotAvailable, QueryCanceled
from psycopg_pool import ConnectionPool, PoolTimeout

from backend.core.settings import SqlAssistantSettings
from backend.sql_assistant.contracts import (
    CompiledSqlQuery,
    SqlCatalog,
    SqlCatalogColumn,
    SqlCatalogRelation,
)


CancellationProbe = Callable[[], bool]
PoolFactory = Callable[..., Any]

_ALLOWED_RELKINDS = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
}
_DISCOVERED_RELKINDS = tuple(sorted({*_ALLOWED_RELKINDS, "m", "f", "S"}))
_MASKED_VALUE = "***"


class SqlAssistantErrorCode(StrEnum):
    DISABLED = "SQL_ASSISTANT_DISABLED"
    CLOSED = "SQL_ASSISTANT_CLOSED"
    POLICY_DENIED = "SQL_POLICY_DENIED"
    DATABASE_UNAVAILABLE = "SQL_DATABASE_UNAVAILABLE"
    POOL_EXHAUSTED = "SQL_POOL_EXHAUSTED"
    SECURITY_CHECK_FAILED = "SQL_SECURITY_CHECK_FAILED"
    QUERY_CANCELLED = "SQL_QUERY_CANCELLED"
    QUERY_TIMEOUT = "SQL_QUERY_TIMEOUT"
    PLAN_REJECTED = "SQL_PLAN_REJECTED"
    RESULT_LIMIT_EXCEEDED = "SQL_RESULT_LIMIT_EXCEEDED"
    EXECUTION_FAILED = "SQL_EXECUTION_FAILED"


_PUBLIC_MESSAGES = {
    SqlAssistantErrorCode.DISABLED: "SQL Assistant 当前不可用",
    SqlAssistantErrorCode.CLOSED: "SQL Assistant 已关闭",
    SqlAssistantErrorCode.POLICY_DENIED: "SQL 查询不符合只读策略",
    SqlAssistantErrorCode.DATABASE_UNAVAILABLE: "SQL 数据源暂时不可用",
    SqlAssistantErrorCode.POOL_EXHAUSTED: "SQL 数据源当前繁忙，请稍后重试",
    SqlAssistantErrorCode.SECURITY_CHECK_FAILED: "SQL 数据源未满足只读安全要求",
    SqlAssistantErrorCode.QUERY_CANCELLED: "SQL 查询已取消",
    SqlAssistantErrorCode.QUERY_TIMEOUT: "SQL 查询超时",
    SqlAssistantErrorCode.PLAN_REJECTED: "SQL 查询计划超出安全预算",
    SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED: "SQL 查询结果超出安全预算",
    SqlAssistantErrorCode.EXECUTION_FAILED: "SQL 查询执行失败",
}


class SqlAssistantError(RuntimeError):
    """Stable, redacted error allowed to cross the Tool Adapter seam."""

    def __init__(
        self,
        code: SqlAssistantErrorCode | str,
        *,
        retryable: bool = False,
    ) -> None:
        normalized = SqlAssistantErrorCode(code)
        self.code = normalized.value
        self.retryable = retryable
        super().__init__(_PUBLIC_MESSAGES[normalized])


@dataclass(frozen=True)
class SqlPlanEstimate:
    total_cost: float
    estimated_rows: int
    estimated_bytes: int


@dataclass(frozen=True)
class SqlQueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...] = field(repr=False)
    row_count: int
    truncated: bool
    result_bytes: int
    estimate: SqlPlanEstimate
    masked_column_count: int
    query_fingerprint: str
    limit_applied: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
        }

    @property
    def observability_metadata(self) -> dict[str, Any]:
        return {
            "query_fingerprint": self.query_fingerprint,
            "row_count": self.row_count,
            "column_count": len(self.columns),
            "result_bytes": self.result_bytes,
            "estimated_cost": self.estimate.total_cost,
            "masked_column_count": self.masked_column_count,
            "limit_applied": self.limit_applied,
        }


@dataclass(frozen=True)
class PostgresAdapterReadiness:
    started: bool
    closed: bool
    ready: bool
    pool_size: int
    pool_available: int
    last_error_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "closed": self.closed,
            "ready": self.ready,
            "pool_size": self.pool_size,
            "pool_available": self.pool_available,
            "last_error_code": self.last_error_code,
        }


@dataclass
class _CancellationState:
    reason: SqlAssistantErrorCode | None = None


@dataclass(frozen=True)
class _RelationRecord:
    oid: int
    schema: str
    name: str
    relkind: str
    owner: str
    can_select: bool
    has_any_column_select: bool
    row_security: bool
    options: tuple[str, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


def _raise(
    code: SqlAssistantErrorCode,
    *,
    retryable: bool = False,
) -> None:
    raise SqlAssistantError(code, retryable=retryable)


def _guard_cancelled(
    *,
    deadline_at: float | None,
    cancellation_probe: CancellationProbe | None,
) -> None:
    try:
        cancelled = bool(cancellation_probe and cancellation_probe())
    except Exception:
        cancelled = True
    if cancelled:
        _raise(SqlAssistantErrorCode.QUERY_CANCELLED)
    if deadline_at is not None and time.monotonic() >= deadline_at:
        _raise(SqlAssistantErrorCode.QUERY_TIMEOUT, retryable=True)


def _effective_timeout_seconds(
    configured: float,
    *,
    deadline_at: float | None,
    cancellation_probe: CancellationProbe | None,
) -> float:
    _guard_cancelled(
        deadline_at=deadline_at,
        cancellation_probe=cancellation_probe,
    )
    if deadline_at is None:
        return configured
    return max(min(configured, deadline_at - time.monotonic()), 0.001)


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 32:
        _raise(SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Enum):
        return _json_value(value.value, depth=depth + 1)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _raise(SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED)
            normalized[key] = _json_value(item, depth=depth + 1)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_json_value(item, depth=depth + 1) for item in value]
    try:
        return str(value)
    except Exception:
        _raise(SqlAssistantErrorCode.EXECUTION_FAILED)


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _raise(SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED)


def _numeric_plan_value(value: Any) -> float:
    if isinstance(value, bool):
        _raise(SqlAssistantErrorCode.PLAN_REJECTED)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        _raise(SqlAssistantErrorCode.PLAN_REJECTED)
    if not math.isfinite(parsed) or parsed < 0:
        _raise(SqlAssistantErrorCode.PLAN_REJECTED)
    return parsed


def _plan_nodes(root: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    stack: list[Mapping[str, Any]] = [root]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > 10_000:
            _raise(SqlAssistantErrorCode.PLAN_REJECTED)
        yield node
        children = node.get("Plans", ())
        if not isinstance(children, Sequence) or isinstance(children, str):
            _raise(SqlAssistantErrorCode.PLAN_REJECTED)
        for child in reversed(children):
            if not isinstance(child, Mapping):
                _raise(SqlAssistantErrorCode.PLAN_REJECTED)
            stack.append(child)


def _parse_plan(raw: Any) -> SqlPlanEstimate:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            _raise(SqlAssistantErrorCode.PLAN_REJECTED)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        _raise(SqlAssistantErrorCode.PLAN_REJECTED)
    envelope = raw[0]
    if not isinstance(envelope, Mapping):
        _raise(SqlAssistantErrorCode.PLAN_REJECTED)
    plan = envelope.get("Plan")
    if not isinstance(plan, Mapping):
        _raise(SqlAssistantErrorCode.PLAN_REJECTED)

    total_cost = 0.0
    estimated_rows = 0
    estimated_bytes = 0
    for node in _plan_nodes(plan):
        cost = _numeric_plan_value(node.get("Total Cost"))
        rows = math.ceil(_numeric_plan_value(node.get("Plan Rows")))
        width = math.ceil(_numeric_plan_value(node.get("Plan Width")))
        total_cost = max(total_cost, cost)
        estimated_rows = max(estimated_rows, rows)
        estimated_bytes = max(estimated_bytes, rows * width)
    return SqlPlanEstimate(
        total_cost=total_cost,
        estimated_rows=estimated_rows,
        estimated_bytes=estimated_bytes,
    )


class PostgresSqlAssistantAdapter:
    """Concrete PostgreSQL Adapter behind the SQL Assistant database seam."""

    def __init__(
        self,
        *,
        settings: SqlAssistantSettings,
        pool_factory: PoolFactory = ConnectionPool,
    ) -> None:
        self.settings = settings
        self._pool_factory = pool_factory
        self._pool: Any | None = None
        self._lock = threading.RLock()
        self._started = False
        self._closed = False
        self._last_error_code: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._closed:
                _raise(SqlAssistantErrorCode.CLOSED)
            if self._started:
                return
            if not self.settings.enabled:
                _raise(SqlAssistantErrorCode.DISABLED)
            if not self.settings.strict_privilege_check:
                _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            if any(
                schema in {"pg_catalog", "information_schema"}
                or schema.startswith("pg_")
                for schema in self.settings.allowed_schemas
            ):
                _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

            pool: Any | None = None
            try:
                pool = self._pool_factory(
                    conninfo=self.settings.dsn.get_secret_value(),
                    kwargs={
                        "connect_timeout": max(
                            1,
                            math.ceil(self.settings.connect_timeout_seconds),
                        ),
                        "application_name": "supermew-sql-assistant",
                    },
                    min_size=self.settings.pool_min_size,
                    max_size=self.settings.pool_max_size,
                    open=False,
                    check=ConnectionPool.check_connection,
                    name="supermew-sql-assistant",
                    timeout=self.settings.pool_timeout_seconds,
                    max_lifetime=self.settings.pool_max_lifetime_seconds,
                )
                pool.open(
                    wait=True,
                    timeout=self.settings.connect_timeout_seconds,
                )
                self._pool = pool
                with self._transaction(
                    timeout_seconds=self.settings.schema_timeout_seconds,
                    deadline_at=None,
                    cancellation_probe=None,
                ):
                    pass
            except SqlAssistantError as exc:
                self._last_error_code = exc.code
                if pool is not None:
                    self._close_pool(pool)
                self._pool = None
                raise
            except Exception:
                self._last_error_code = SqlAssistantErrorCode.DATABASE_UNAVAILABLE.value
                if pool is not None:
                    self._close_pool(pool)
                self._pool = None
                _raise(
                    SqlAssistantErrorCode.DATABASE_UNAVAILABLE,
                    retryable=True,
                )
            self._started = True
            self._last_error_code = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            pool = self._pool
            self._pool = None
            self._started = False
            self._closed = True
        if pool is not None:
            self._close_pool(pool)

    def readiness(self) -> PostgresAdapterReadiness:
        with self._lock:
            pool = self._pool
            started = self._started
            closed = self._closed
            last_error_code = self._last_error_code
        stats: Mapping[str, Any] = {}
        if pool is not None:
            try:
                stats = pool.get_stats()
            except Exception:
                stats = {}
        return PostgresAdapterReadiness(
            started=started,
            closed=closed,
            ready=started and not closed and pool is not None,
            pool_size=int(stats.get("pool_size", 0) or 0),
            pool_available=int(stats.get("pool_available", 0) or 0),
            last_error_code=last_error_code,
        )

    def load_catalog(
        self,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> SqlCatalog:
        self._ensure_started()
        state = _CancellationState()
        try:
            with self._transaction(
                timeout_seconds=self.settings.schema_timeout_seconds,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            ) as (connection, cursor):
                with self._cancellation_watch(
                    connection,
                    state=state,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                ):
                    database_name = self._database_name(cursor)
                    relations = self._load_relation_records(cursor)
                    catalog_relations = self._load_catalog_relations(
                        cursor,
                        relations,
                    )
                    revision = self._catalog_revision(catalog_relations)
                if state.reason is not None:
                    _raise(
                        state.reason,
                        retryable=(state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT),
                    )
                return SqlCatalog(
                    relations=tuple(catalog_relations),
                    database=database_name,
                    revision=revision,
                )
        except SqlAssistantError:
            raise
        except PoolTimeout:
            _raise(SqlAssistantErrorCode.POOL_EXHAUSTED, retryable=True)
        except QueryCanceled:
            if state.reason is not None:
                _raise(
                    state.reason,
                    retryable=state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT,
                )
            self._raise_cancel_or_timeout(
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
        except Exception:
            if state.reason is not None:
                _raise(
                    state.reason,
                    retryable=state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT,
                )
            _raise(SqlAssistantErrorCode.DATABASE_UNAVAILABLE, retryable=True)

    def execute(
        self,
        compiled: CompiledSqlQuery,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> SqlQueryResult:
        self._ensure_started()
        state = _CancellationState()
        try:
            with self._transaction(
                timeout_seconds=self.settings.statement_timeout_seconds,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            ) as (connection, cursor):
                physical_relations = self._compiled_relations(compiled)
                if physical_relations:
                    current = self._load_relation_records(
                        cursor,
                        selected=physical_relations,
                    )
                    current_names = {record.qualified_name for record in current}
                    if current_names != set(physical_relations):
                        _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

                with self._cancellation_watch(
                    connection,
                    state=state,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                ):
                    estimate = self._explain(cursor, compiled)
                    self._validate_estimate(estimate)
                    result = self._execute_cursor(
                        connection,
                        compiled,
                        estimate=estimate,
                        state=state,
                        deadline_at=deadline_at,
                        cancellation_probe=cancellation_probe,
                    )
                if state.reason is not None:
                    _raise(
                        state.reason,
                        retryable=state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT,
                    )
                return result
        except SqlAssistantError:
            raise
        except PoolTimeout:
            _raise(SqlAssistantErrorCode.POOL_EXHAUSTED, retryable=True)
        except (QueryCanceled, LockNotAvailable):
            reason = state.reason or SqlAssistantErrorCode.QUERY_TIMEOUT
            _raise(
                reason,
                retryable=reason is SqlAssistantErrorCode.QUERY_TIMEOUT,
            )
        except psycopg.OperationalError:
            if state.reason is not None:
                _raise(
                    state.reason,
                    retryable=state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT,
                )
            _raise(SqlAssistantErrorCode.DATABASE_UNAVAILABLE, retryable=True)
        except Exception:
            if state.reason is not None:
                _raise(
                    state.reason,
                    retryable=state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT,
                )
            _raise(SqlAssistantErrorCode.EXECUTION_FAILED)

    def _ensure_started(self) -> None:
        with self._lock:
            started = self._started
            closed = self._closed
        if closed:
            _raise(SqlAssistantErrorCode.CLOSED)
        if not started:
            self.start()

    @staticmethod
    def _close_pool(pool: Any) -> None:
        try:
            pool.close(timeout=5.0)
        except Exception:
            pass

    def _require_pool(self) -> Any:
        with self._lock:
            pool = self._pool
        if pool is None:
            _raise(SqlAssistantErrorCode.DATABASE_UNAVAILABLE, retryable=True)
        return pool

    @contextmanager
    def _transaction(
        self,
        *,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Iterator[tuple[Any, Any]]:
        effective_timeout = _effective_timeout_seconds(
            timeout_seconds,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        pool = self._require_pool()
        with pool.connection(timeout=self.settings.pool_timeout_seconds) as connection:
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
            connection.read_only = True
            connection.deferrable = False
            with connection.transaction(force_rollback=True):
                with connection.cursor() as cursor:
                    self._configure_transaction(cursor, effective_timeout)
                    self._validate_environment(cursor)
                    yield connection, cursor

    def _configure_transaction(self, cursor: Any, timeout_seconds: float) -> None:
        statement_ms = max(1, math.ceil(timeout_seconds * 1000))
        lock_ms = max(
            1,
            min(
                math.ceil(self.settings.lock_timeout_seconds * 1000),
                statement_ms,
            ),
        )
        search_path = pg_sql.SQL(", ").join(
            [
                pg_sql.Identifier("pg_catalog"),
                *(
                    pg_sql.Identifier(schema)
                    for schema in self.settings.allowed_schemas
                ),
            ]
        )
        cursor.execute(pg_sql.SQL("SET LOCAL search_path TO {}").format(search_path))
        cursor.execute(
            pg_sql.SQL("SET LOCAL statement_timeout = {}").format(
                pg_sql.Literal(statement_ms)
            )
        )
        cursor.execute(
            pg_sql.SQL("SET LOCAL lock_timeout = {}").format(pg_sql.Literal(lock_ms))
        )
        cursor.execute("SET LOCAL row_security = on")

    def _validate_environment(self, cursor: Any) -> None:
        cursor.execute(
            """
            SELECT current_user,
                   session_user,
                   current_database(),
                   current_setting('transaction_isolation'),
                   current_setting('transaction_read_only'),
                   current_setting('row_security')
            """
        )
        identity = cursor.fetchone()
        if (
            not identity
            or identity[0] != self.settings.expected_role
            or identity[1] != self.settings.expected_role
            or identity[3] != "repeatable read"
            or identity[4] != "on"
            or identity[5] != "on"
        ):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

        cursor.execute(
            """
            SELECT rolsuper,
                   rolcreaterole,
                   rolcreatedb,
                   rolreplication,
                   rolbypassrls,
                   rolcanlogin
              FROM pg_catalog.pg_roles
             WHERE rolname = current_user
            """
        )
        role = cursor.fetchone()
        if not role or any(bool(value) for value in role[:5]) or not bool(role[5]):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_roles AS candidate
             WHERE candidate.rolname <> current_user
               AND (
                    candidate.rolsuper
                    OR candidate.rolcreaterole
                    OR candidate.rolcreatedb
                    OR candidate.rolreplication
                    OR candidate.rolbypassrls
               )
               AND pg_catalog.pg_has_role(
                    current_user,
                    candidate.oid,
                    'MEMBER'
               )
            """
        )
        dangerous_memberships = cursor.fetchone()
        if not dangerous_memberships or int(dangerous_memberships[0]) != 0:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

        cursor.execute(
            """
            SELECT pg_catalog.has_database_privilege(
                       current_user,
                       current_database(),
                       'CREATE'
                   ),
                   pg_catalog.has_database_privilege(
                       current_user,
                       current_database(),
                       'TEMP'
                   )
            """
        )
        database_privileges = cursor.fetchone()
        if (
            not database_privileges
            or bool(database_privileges[0])
            or bool(database_privileges[1])
        ):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

        cursor.execute(
            """
            SELECT namespace.nspname,
                   pg_catalog.has_schema_privilege(
                       current_user,
                       namespace.oid,
                       'USAGE'
                   ),
                   pg_catalog.has_schema_privilege(
                       current_user,
                       namespace.oid,
                       'CREATE'
                   )
              FROM pg_catalog.pg_namespace AS namespace
             WHERE namespace.nspname <> 'information_schema'
               AND namespace.nspname !~ '^pg_'
             ORDER BY namespace.nspname
            """
        )
        schema_rows = cursor.fetchall()
        expected_schemas = set(self.settings.allowed_schemas)
        actual_schemas = {row[0] for row in schema_rows if row[0] in expected_schemas}
        if actual_schemas != expected_schemas:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if any(bool(row[2]) for row in schema_rows):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if any(row[0] in expected_schemas and not bool(row[1]) for row in schema_rows):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

    @staticmethod
    def _database_name(cursor: Any) -> str:
        cursor.execute("SELECT current_database()")
        row = cursor.fetchone()
        if not row or not isinstance(row[0], str) or not row[0]:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        return row[0]

    def _is_allowed_relation(self, schema: str, name: str) -> bool:
        qualified = f"{schema}.{name}"
        return any(
            pattern == qualified or pattern == f"{schema}.*"
            for pattern in self.settings.allowed_tables
        )

    def _load_relation_records(
        self,
        cursor: Any,
        *,
        selected: Sequence[str] | None = None,
    ) -> list[_RelationRecord]:
        cursor.execute(
            """
            SELECT relation.oid,
                   namespace.nspname,
                   relation.relname,
                   relation.relkind,
                   pg_catalog.pg_get_userbyid(relation.relowner),
                   pg_catalog.has_table_privilege(
                       current_user,
                       relation.oid,
                       'SELECT'
                   ),
                   pg_catalog.has_any_column_privilege(
                       current_user,
                       relation.oid,
                       'SELECT'
                   ),
                   relation.relrowsecurity,
                   relation.reloptions
              FROM pg_catalog.pg_class AS relation
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname <> 'information_schema'
               AND namespace.nspname !~ '^pg_'
               AND relation.relkind::text = ANY(%s::text[])
             ORDER BY namespace.nspname, relation.relname
            """,
            (list(_DISCOVERED_RELKINDS),),
        )
        selected_names = set(selected or ())
        records: list[_RelationRecord] = []
        matched_patterns: set[str] = set()
        for row in cursor.fetchall():
            record = _RelationRecord(
                oid=int(row[0]),
                schema=str(row[1]),
                name=str(row[2]),
                relkind=str(row[3]),
                owner=str(row[4]),
                can_select=bool(row[5]),
                has_any_column_select=bool(row[6]),
                row_security=bool(row[7]),
                options=tuple(str(value) for value in (row[8] or ())),
            )
            allowed = self._is_allowed_relation(record.schema, record.name)
            if not allowed:
                if self.settings.strict_privilege_check and (
                    record.can_select or record.has_any_column_select
                ):
                    _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
                continue
            if selected is not None and record.qualified_name not in selected_names:
                continue
            for pattern in self.settings.allowed_tables:
                if pattern in {
                    record.qualified_name,
                    f"{record.schema}.*",
                }:
                    matched_patterns.add(pattern)
            self._validate_relation(record)
            records.append(record)

        actual_names = {record.qualified_name for record in records}
        if selected is not None:
            if actual_names != selected_names:
                _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        elif matched_patterns != set(self.settings.allowed_tables):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if not records and selected is None:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        return records

    def _validate_relation(self, record: _RelationRecord) -> None:
        if not self._is_allowed_relation(record.schema, record.name):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if record.relkind not in _ALLOWED_RELKINDS:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if not record.owner or record.owner == self.settings.expected_role:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if not record.can_select:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if record.relkind in {"r", "p"} and not record.row_security:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
        if record.relkind == "v" and not {
            "security_invoker=true",
            "security_barrier=true",
        }.issubset(record.options):
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

    def _load_catalog_relations(
        self,
        cursor: Any,
        records: Sequence[_RelationRecord],
    ) -> list[SqlCatalogRelation]:
        record_by_oid = {record.oid: record for record in records}
        cursor.execute(
            """
            SELECT attribute.attrelid,
                   attribute.attname,
                   pg_catalog.format_type(
                       attribute.atttypid,
                       attribute.atttypmod
                   ),
                   NOT attribute.attnotnull,
                   attribute.attnum,
                   type_namespace.nspname,
                   data_type.typtype
              FROM pg_catalog.pg_attribute AS attribute
              JOIN pg_catalog.pg_type AS data_type
                ON data_type.oid = attribute.atttypid
              JOIN pg_catalog.pg_namespace AS type_namespace
                ON type_namespace.oid = data_type.typnamespace
             WHERE attribute.attrelid = ANY(%s::oid[])
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
             ORDER BY attribute.attrelid, attribute.attnum
            """,
            (list(record_by_oid),),
        )
        columns_by_oid: dict[int, list[SqlCatalogColumn]] = {
            oid: [] for oid in record_by_oid
        }
        sensitive = set(self.settings.sensitive_columns)
        matched_sensitive: set[str] = set()
        for row in cursor.fetchall():
            oid = int(row[0])
            record = record_by_oid.get(oid)
            if record is None:
                _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            self._validate_column_type(
                namespace=str(row[5]),
                type_kind=str(row[6]),
            )
            column_name = str(row[1])
            qualified_column = f"{record.schema}.{record.name}.{column_name}"
            is_sensitive = qualified_column in sensitive
            if is_sensitive:
                matched_sensitive.add(qualified_column)
            columns_by_oid[oid].append(
                SqlCatalogColumn(
                    name=column_name,
                    data_type=str(row[2]),
                    nullable=bool(row[3]),
                    sensitive=is_sensitive,
                )
            )

        if matched_sensitive != sensitive:
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

        catalog_relations: list[SqlCatalogRelation] = []
        for record in records:
            columns = columns_by_oid.get(record.oid, [])
            if not columns:
                _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            catalog_relations.append(
                SqlCatalogRelation(
                    schema=record.schema,
                    name=record.name,
                    columns=tuple(columns),
                    kind=_ALLOWED_RELKINDS[record.relkind],
                    owner=record.owner,
                )
            )
        return catalog_relations

    @staticmethod
    def _validate_column_type(*, namespace: str, type_kind: str) -> None:
        # A user-defined domain/composite/enum can redirect even ordinary
        # operators to extension code.  The SQL Assistant currently admits
        # only PostgreSQL's built-in base (including built-in array) types.
        if namespace != "pg_catalog" or type_kind != "b":
            _raise(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

    @staticmethod
    def _catalog_revision(relations: Sequence[SqlCatalogRelation]) -> str:
        projection = [
            {
                "schema": relation.schema,
                "name": relation.name,
                "kind": relation.kind,
                "owner": relation.owner,
                "columns": [
                    {
                        "name": column.name,
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                        "sensitive": column.sensitive,
                    }
                    for column in relation.columns
                ],
            }
            for relation in relations
        ]
        return hashlib.sha256(_json_bytes(projection)).hexdigest()

    @staticmethod
    def _compiled_relations(compiled: CompiledSqlQuery) -> tuple[str, ...]:
        relations: list[str] = []
        for reference in compiled.relations:
            schema = str(reference.schema)
            table = str(reference.table)
            qualified = f"{schema}.{table}"
            if qualified not in relations:
                relations.append(qualified)
        return tuple(relations)

    def _explain(
        self,
        cursor: Any,
        compiled: CompiledSqlQuery,
    ) -> SqlPlanEstimate:
        explain = pg_sql.SQL(
            "EXPLAIN (FORMAT JSON, COSTS TRUE, VERBOSE FALSE, "
            "SETTINGS FALSE, SUMMARY FALSE) {}"
        ).format(pg_sql.SQL(compiled.executable_sql))
        cursor.execute(explain, compiled.parameters or None)
        row = cursor.fetchone()
        if not row:
            _raise(SqlAssistantErrorCode.PLAN_REJECTED)
        return _parse_plan(row[0])

    def _validate_estimate(self, estimate: SqlPlanEstimate) -> None:
        if (
            estimate.total_cost > self.settings.max_estimated_cost
            or estimate.estimated_rows > self.settings.max_estimated_rows
            or estimate.estimated_bytes > self.settings.max_estimated_bytes
        ):
            _raise(SqlAssistantErrorCode.PLAN_REJECTED)

    def _execute_cursor(
        self,
        connection: Any,
        compiled: CompiledSqlQuery,
        *,
        estimate: SqlPlanEstimate,
        state: _CancellationState,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> SqlQueryResult:
        cursor_name = f"sql_assistant_{uuid.uuid4().hex}"
        with connection.cursor(name=cursor_name) as result_cursor:
            result_cursor.itersize = self.settings.fetch_size
            result_cursor.execute(
                compiled.executable_sql,
                compiled.parameters or None,
            )
            description = result_cursor.description or ()
            columns = tuple(str(column.name) for column in description)
            masked_ordinals = frozenset(
                int(value) for value in compiled.masked_ordinals
            )
            if any(index < 0 or index >= len(columns) for index in masked_ordinals):
                _raise(SqlAssistantErrorCode.EXECUTION_FAILED)

            rows: list[tuple[Any, ...]] = []
            truncated = False
            running_result_bytes = len(
                _json_bytes(
                    {
                        "columns": list(columns),
                        "rows": [],
                        "row_count": compiled.max_rows,
                        "truncated": True,
                    }
                )
            )
            while len(rows) <= compiled.max_rows:
                _guard_cancelled(
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
                if state.reason is not None:
                    _raise(
                        state.reason,
                        retryable=(state.reason is SqlAssistantErrorCode.QUERY_TIMEOUT),
                    )
                remaining_probe = compiled.max_rows + 1 - len(rows)
                if remaining_probe <= 0:
                    break
                batch = result_cursor.fetchmany(
                    min(self.settings.fetch_size, remaining_probe)
                )
                if not batch:
                    break
                for raw_row in batch:
                    if len(raw_row) != len(columns):
                        _raise(SqlAssistantErrorCode.EXECUTION_FAILED)
                    if len(rows) >= compiled.max_rows:
                        truncated = True
                        break
                    normalized: list[Any] = []
                    for ordinal, value in enumerate(raw_row):
                        cell = (
                            _MASKED_VALUE
                            if ordinal in masked_ordinals
                            else _json_value(value)
                        )
                        if len(_json_bytes(cell)) > self.settings.max_cell_bytes:
                            _raise(SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED)
                        normalized.append(cell)
                    normalized_row = tuple(normalized)
                    running_result_bytes += len(_json_bytes(normalized_row)) + 1
                    if running_result_bytes > self.settings.max_result_bytes:
                        _raise(SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED)
                    rows.append(normalized_row)
                if truncated:
                    break

            payload = {
                "columns": list(columns),
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
                "truncated": truncated,
            }
            result_bytes = len(_json_bytes(payload))
            if result_bytes > self.settings.max_result_bytes:
                _raise(SqlAssistantErrorCode.RESULT_LIMIT_EXCEEDED)
            return SqlQueryResult(
                columns=columns,
                rows=tuple(rows),
                row_count=len(rows),
                truncated=truncated,
                result_bytes=result_bytes,
                estimate=estimate,
                masked_column_count=len(masked_ordinals),
                query_fingerprint=compiled.shape_fingerprint,
                limit_applied=compiled.limit_applied,
            )

    @contextmanager
    def _cancellation_watch(
        self,
        connection: Any,
        *,
        state: _CancellationState,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Iterator[None]:
        if deadline_at is None and cancellation_probe is None:
            yield
            return

        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(0.025):
                reason: SqlAssistantErrorCode | None = None
                try:
                    if cancellation_probe is not None and cancellation_probe():
                        reason = SqlAssistantErrorCode.QUERY_CANCELLED
                except Exception:
                    reason = SqlAssistantErrorCode.QUERY_CANCELLED
                if reason is None and deadline_at is not None:
                    if time.monotonic() >= deadline_at:
                        reason = SqlAssistantErrorCode.QUERY_TIMEOUT
                if reason is None:
                    continue
                state.reason = reason
                try:
                    cancel_safe = getattr(connection, "cancel_safe", None)
                    if callable(cancel_safe):
                        cancel_safe(timeout=1.0)
                    else:
                        connection.cancel()
                except Exception:
                    pass
                return

        watcher = threading.Thread(
            target=watch,
            name="sql-assistant-cancellation",
            daemon=True,
        )
        watcher.start()
        try:
            yield
        finally:
            stop.set()
            watcher.join(timeout=0.2)

    @staticmethod
    def _raise_cancel_or_timeout(
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> None:
        try:
            if cancellation_probe is not None and cancellation_probe():
                _raise(SqlAssistantErrorCode.QUERY_CANCELLED)
        except SqlAssistantError:
            raise
        except Exception:
            _raise(SqlAssistantErrorCode.QUERY_CANCELLED)
        if deadline_at is not None and time.monotonic() >= deadline_at:
            _raise(SqlAssistantErrorCode.QUERY_TIMEOUT, retryable=True)
        _raise(SqlAssistantErrorCode.QUERY_TIMEOUT, retryable=True)


__all__ = [
    "PostgresAdapterReadiness",
    "PostgresSqlAssistantAdapter",
    "SqlAssistantError",
    "SqlAssistantErrorCode",
    "SqlPlanEstimate",
    "SqlQueryResult",
]
