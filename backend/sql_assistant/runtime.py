"""Process-wide runtime for the read-only SQL Assistant Module."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from backend.core.settings import SqlAssistantSettings, get_settings
from backend.sql_assistant.contracts import (
    CompiledSqlQuery,
    SqlCatalog,
    SqlCatalogRelation,
    SqlPolicy,
    SqlPolicyError,
)
from backend.sql_assistant.policy import DEFAULT_ALLOWED_FUNCTIONS, SqlPolicyCompiler
from backend.sql_assistant.postgres import (
    CancellationProbe,
    PostgresSqlAssistantAdapter,
    SqlAssistantError,
    SqlAssistantErrorCode,
    SqlQueryResult,
)


_QUALIFIED_RELATION = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]{0,62}\."
    r"[A-Za-z_][A-Za-z0-9_$]{0,62}$"
)
_UNQUALIFIED_RELATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


class SqlDatabaseAdapter(Protocol):
    """Small database Interface used by the orchestration runtime."""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def readiness(self) -> object: ...

    def load_catalog(
        self,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> SqlCatalog: ...

    def execute(
        self,
        compiled: CompiledSqlQuery,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> SqlQueryResult: ...


class SqlCompiler(Protocol):
    def compile(self, sql: str, catalog: SqlCatalog) -> CompiledSqlQuery: ...


@dataclass(frozen=True)
class SqlAssistantReadiness:
    enabled: bool
    started: bool
    closed: bool
    ready: bool
    catalog_cached: bool
    catalog_age_seconds: float | None
    catalog_hash: str | None
    database: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "started": self.started,
            "closed": self.closed,
            "ready": self.ready,
            "catalog_cached": self.catalog_cached,
            "catalog_age_seconds": self.catalog_age_seconds,
            "catalog_hash": self.catalog_hash,
            "database": dict(self.database),
        }


@dataclass(frozen=True)
class _CatalogSnapshot:
    catalog: SqlCatalog
    loaded_at: float


AdapterFactory = Callable[[SqlAssistantSettings], SqlDatabaseAdapter]
CompilerFactory = Callable[[SqlPolicy], SqlCompiler]


class SqlAssistantRuntime:
    """Deep Module hiding policy, catalog cache, pool, and result projection."""

    def __init__(
        self,
        *,
        settings: SqlAssistantSettings,
        adapter_factory: AdapterFactory | None = None,
        compiler_factory: CompilerFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._policy = SqlPolicy(
            default_schema=settings.allowed_schemas[0]
            if settings.allowed_schemas
            else "public",
            allowed_schemas=frozenset(settings.allowed_schemas),
            allowed_tables=frozenset(settings.allowed_tables),
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS,
            sensitive_columns=frozenset(settings.sensitive_columns),
            max_rows=settings.max_rows,
            max_tables=settings.max_tables_per_query,
            max_ast_nodes=settings.max_ast_nodes,
        )
        self._adapter = (adapter_factory or _postgres_adapter)(settings)
        self._compiler = (compiler_factory or SqlPolicyCompiler)(self._policy)
        self._monotonic = monotonic
        self._lifecycle_lock = threading.RLock()
        self._catalog_lock = threading.RLock()
        self._catalog: _CatalogSnapshot | None = None
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise SqlAssistantError(SqlAssistantErrorCode.CLOSED)
            if self._started:
                return
            if not self.settings.enabled:
                raise SqlAssistantError(SqlAssistantErrorCode.DISABLED)
            if not self.settings.strict_privilege_check:
                raise SqlAssistantError(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            try:
                self._adapter.start()
                catalog = self._adapter.load_catalog()
            except Exception:
                self._adapter.close()
                raise
            with self._catalog_lock:
                self._catalog = _CatalogSnapshot(
                    catalog=catalog,
                    loaded_at=self._monotonic(),
                )
            self._started = True

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._adapter.close()
            self._started = False
            self._closed = True
            with self._catalog_lock:
                self._catalog = None

    def readiness(self) -> SqlAssistantReadiness:
        now = self._monotonic()
        with self._lifecycle_lock:
            started = self._started
            closed = self._closed
        with self._catalog_lock:
            snapshot = self._catalog
        adapter_readiness = self._adapter.readiness()
        if hasattr(adapter_readiness, "to_dict"):
            database = adapter_readiness.to_dict()
        elif isinstance(adapter_readiness, Mapping):
            database = dict(adapter_readiness)
        else:
            database = {}
        catalog_age = None if snapshot is None else max(now - snapshot.loaded_at, 0.0)
        database_ready = bool(database.get("ready", started and not closed))
        return SqlAssistantReadiness(
            enabled=self.settings.enabled,
            started=started,
            closed=closed,
            ready=(
                self.settings.enabled
                and started
                and not closed
                and snapshot is not None
                and database_ready
            ),
            catalog_cached=snapshot is not None,
            catalog_age_seconds=catalog_age,
            catalog_hash=(
                snapshot.catalog.fingerprint if snapshot is not None else None
            ),
            database=database,
        )

    def describe_schema(self, tables: tuple[str, ...]) -> dict[str, Any]:
        catalog, cache_hit = self._catalog_snapshot()
        selected = self._select_relations(catalog, tables)
        schemas = {relation.schema for relation in selected}
        column_count = sum(len(relation.columns) for relation in selected)
        return {
            "tables": [self._relation_payload(relation) for relation in selected],
            "observability_metadata": {
                "schema_count": len(schemas),
                "table_count": len(selected),
                "column_count": column_count,
                "catalog_cache_hit": cache_hit,
            },
        }

    def query(
        self,
        sql: str,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> dict[str, Any]:
        self._guard_request(
            sql,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        catalog, _cache_hit = self._catalog_snapshot(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        try:
            compiled = self._compiler.compile(sql, catalog)
        except SqlPolicyError:
            raise SqlAssistantError(SqlAssistantErrorCode.POLICY_DENIED) from None
        result = self._adapter.execute(
            compiled,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        payload = result.to_dict()
        payload["observability_metadata"] = result.observability_metadata
        return payload

    def _catalog_snapshot(
        self,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> tuple[SqlCatalog, bool]:
        self._ensure_started()
        now = self._monotonic()
        with self._catalog_lock:
            snapshot = self._catalog
            if (
                snapshot is not None
                and now - snapshot.loaded_at < self.settings.catalog_cache_ttl_seconds
            ):
                return snapshot.catalog, True

            catalog = self._adapter.load_catalog(
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            self._catalog = _CatalogSnapshot(catalog=catalog, loaded_at=now)
            return catalog, False

    def _ensure_started(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise SqlAssistantError(SqlAssistantErrorCode.CLOSED)
            if self._started:
                return
        self.start()

    def _guard_request(
        self,
        sql: str,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> None:
        if not isinstance(sql, str) or not sql.strip():
            raise SqlAssistantError(SqlAssistantErrorCode.POLICY_DENIED)
        if len(sql) > self.settings.max_sql_characters:
            raise SqlAssistantError(SqlAssistantErrorCode.POLICY_DENIED)
        try:
            if cancellation_probe is not None and cancellation_probe():
                raise SqlAssistantError(SqlAssistantErrorCode.QUERY_CANCELLED)
        except SqlAssistantError:
            raise
        except Exception:
            raise SqlAssistantError(SqlAssistantErrorCode.QUERY_CANCELLED) from None
        if deadline_at is not None and self._monotonic() >= deadline_at:
            raise SqlAssistantError(
                SqlAssistantErrorCode.QUERY_TIMEOUT,
                retryable=True,
            )

    @staticmethod
    def _select_relations(
        catalog: SqlCatalog,
        requested: tuple[str, ...],
    ) -> tuple[SqlCatalogRelation, ...]:
        relations = tuple(
            sorted(catalog.relations, key=lambda item: (item.schema, item.name))
        )
        if not requested:
            return relations
        if len(requested) > 128 or len(set(requested)) != len(requested):
            raise SqlAssistantError(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)

        selected: list[SqlCatalogRelation] = []
        for raw_name in requested:
            if not isinstance(raw_name, str) or raw_name != raw_name.strip():
                raise SqlAssistantError(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            if _QUALIFIED_RELATION.fullmatch(raw_name):
                matches = [
                    relation
                    for relation in relations
                    if relation.qualified_name == raw_name
                ]
            elif _UNQUALIFIED_RELATION.fullmatch(raw_name):
                matches = [
                    relation for relation in relations if relation.name == raw_name
                ]
            else:
                raise SqlAssistantError(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            if len(matches) != 1:
                raise SqlAssistantError(SqlAssistantErrorCode.SECURITY_CHECK_FAILED)
            if matches[0] not in selected:
                selected.append(matches[0])
        return tuple(selected)

    @staticmethod
    def _relation_payload(relation: SqlCatalogRelation) -> dict[str, Any]:
        return {
            "schema": relation.schema,
            "name": relation.name,
            "kind": relation.kind.value,
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


def _postgres_adapter(settings: SqlAssistantSettings) -> SqlDatabaseAdapter:
    return PostgresSqlAssistantAdapter(settings=settings)


@lru_cache(maxsize=1)
def get_sql_assistant_runtime() -> SqlAssistantRuntime:
    """Return the lazy process-wide SQL runtime without exposing its DSN."""

    return SqlAssistantRuntime(settings=get_settings().sql_assistant)


__all__ = [
    "SqlAssistantReadiness",
    "SqlAssistantRuntime",
    "SqlDatabaseAdapter",
    "get_sql_assistant_runtime",
]
