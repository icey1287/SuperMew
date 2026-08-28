from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FUNCTION_PART_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_POSTGRES_FUNCTION_ALIASES = {
    "ceiling": "ceil",
    "date_trunc": "timestamp_trunc",
    "now": "current_timestamp",
}


def _canonical_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    compact = value.strip()
    if not compact or compact != value:
        raise ValueError(f"{field} must be a non-empty canonical identifier")
    if "." in compact or "\x00" in compact:
        raise ValueError(f"{field} cannot contain dots or NUL bytes")
    if len(compact.encode("utf-8")) > 63:
        raise ValueError(f"{field} exceeds PostgreSQL's identifier limit")
    return compact


def _optional_text(value: str | None, *, field: str, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    compact = value.strip()
    if not compact or "\x00" in compact:
        raise ValueError(f"{field} must be non-empty and cannot contain NUL bytes")
    if len(compact.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds its size limit")
    return compact


def _qualified_table_rule(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("allowed_tables entries must be strings")
    compact = value.strip()
    parts = compact.split(".")
    if len(parts) != 2:
        raise ValueError("allowed_tables entries must use schema.table")
    schema, table = parts
    _identifier(schema, field="allowed table schema")
    if table != "*":
        _identifier(table, field="allowed table name")
    return compact


def _qualified_column_rule(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("sensitive_columns entries must be strings")
    compact = value.strip()
    parts = compact.split(".")
    if len(parts) != 3:
        raise ValueError("sensitive_columns entries must use schema.table.column")
    _identifier(parts[0], field="sensitive column schema")
    _identifier(parts[1], field="sensitive column table")
    _identifier(parts[2], field="sensitive column name")
    return compact


def _function_rule(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("allowed_functions entries must be strings")
    compact = value.strip().lower()
    parts = compact.split(".")
    if not 1 <= len(parts) <= 2 or any(
        not _FUNCTION_PART_RE.fullmatch(part) for part in parts
    ):
        raise ValueError(
            "allowed_functions entries must use function or schema.function"
        )
    if len(parts) == 1:
        return _POSTGRES_FUNCTION_ALIASES.get(compact, compact)
    return compact


def _positive_int(value: int, *, field: str, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if not 1 <= value <= upper:
        raise ValueError(f"{field} must be between 1 and {upper}")
    return value


class SqlRelationKind(StrEnum):
    TABLE = "table"
    PARTITIONED_TABLE = "partitioned_table"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    FOREIGN_TABLE = "foreign_table"


class SqlPolicyCode(StrEnum):
    INVALID_SQL = "INVALID_SQL"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    STATEMENT_NOT_ALLOWED = "STATEMENT_NOT_ALLOWED"
    AST_LIMIT_EXCEEDED = "AST_LIMIT_EXCEEDED"
    TABLE_LIMIT_EXCEEDED = "TABLE_LIMIT_EXCEEDED"
    RELATION_DENIED = "RELATION_DENIED"
    TABLE_FUNCTION_DENIED = "TABLE_FUNCTION_DENIED"
    FUNCTION_DENIED = "FUNCTION_DENIED"
    OPERATOR_DENIED = "OPERATOR_DENIED"
    WILDCARD_DENIED = "WILDCARD_DENIED"
    PLACEHOLDER_DENIED = "PLACEHOLDER_DENIED"
    COLUMN_DENIED = "COLUMN_DENIED"
    SENSITIVE_USAGE_DENIED = "SENSITIVE_USAGE_DENIED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"


class SqlPolicyError(ValueError):
    """Stable, redacted policy failure safe to map to ``POLICY_DENIED``."""

    def __init__(
        self,
        code: SqlPolicyCode | str,
        message: str,
        *,
        safe_details: dict[str, str | int] | None = None,
    ) -> None:
        self.code = SqlPolicyCode(code)
        self.safe_details = dict(safe_details or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SqlCatalogColumn:
    name: str
    data_type: str = "UNKNOWN"
    nullable: bool = True
    sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, field="column name"))
        data_type = _optional_text(
            self.data_type,
            field="column data_type",
            max_bytes=256,
        )
        object.__setattr__(self, "data_type", data_type or "UNKNOWN")
        if not isinstance(self.nullable, bool):
            raise TypeError("column nullable must be a bool")
        if not isinstance(self.sensitive, bool):
            raise TypeError("column sensitive must be a bool")


@dataclass(frozen=True, slots=True)
class SqlCatalogRelation:
    schema: str
    name: str
    columns: tuple[SqlCatalogColumn, ...]
    kind: SqlRelationKind = SqlRelationKind.TABLE
    owner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _identifier(self.schema, field="schema"))
        object.__setattr__(self, "name", _identifier(self.name, field="table"))
        columns = tuple(self.columns)
        if any(not isinstance(column, SqlCatalogColumn) for column in columns):
            raise TypeError("relation columns must contain SqlCatalogColumn values")
        names = [column.name for column in columns]
        if len(names) != len(set(names)):
            raise ValueError("relation column names must be unique")
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "kind", SqlRelationKind(self.kind))
        object.__setattr__(
            self,
            "owner",
            _optional_text(self.owner, field="relation owner", max_bytes=256),
        )

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    def column(self, name: str) -> SqlCatalogColumn | None:
        return next((column for column in self.columns if column.name == name), None)


@dataclass(frozen=True, slots=True)
class SqlCatalog:
    relations: tuple[SqlCatalogRelation, ...]
    database: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        relations = tuple(self.relations)
        if any(not isinstance(relation, SqlCatalogRelation) for relation in relations):
            raise TypeError("catalog relations must contain SqlCatalogRelation values")
        identities = [(relation.schema, relation.name) for relation in relations]
        if len(identities) != len(set(identities)):
            raise ValueError("catalog relation identities must be unique")
        object.__setattr__(self, "relations", relations)
        object.__setattr__(
            self,
            "database",
            _optional_text(self.database, field="catalog database", max_bytes=256),
        )
        object.__setattr__(
            self,
            "revision",
            _optional_text(self.revision, field="catalog revision", max_bytes=512),
        )

    def relation(self, schema: str, table: str) -> SqlCatalogRelation | None:
        return next(
            (
                relation
                for relation in self.relations
                if relation.schema == schema and relation.name == table
            ),
            None,
        )

    @property
    def fingerprint(self) -> str:
        relations = sorted(self.relations, key=lambda item: (item.schema, item.name))
        return _canonical_fingerprint(
            {
                "database": self.database,
                "revision": self.revision,
                "relations": [
                    {
                        "schema": relation.schema,
                        "name": relation.name,
                        "kind": relation.kind.value,
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
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class SqlPolicy:
    default_schema: str = "public"
    allowed_schemas: frozenset[str] = frozenset()
    allowed_tables: frozenset[str] = frozenset()
    allowed_functions: frozenset[str] = frozenset()
    sensitive_columns: frozenset[str] = frozenset()
    max_rows: int = 200
    max_tables: int = 8
    max_ast_nodes: int = 1_000

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_schema",
            _identifier(self.default_schema, field="default_schema"),
        )
        schemas = frozenset(
            _identifier(schema, field="allowed schema")
            for schema in self.allowed_schemas
        )
        tables = frozenset(_qualified_table_rule(rule) for rule in self.allowed_tables)
        functions = frozenset(_function_rule(rule) for rule in self.allowed_functions)
        sensitive = frozenset(
            _qualified_column_rule(rule) for rule in self.sensitive_columns
        )
        object.__setattr__(self, "allowed_schemas", schemas)
        object.__setattr__(self, "allowed_tables", tables)
        object.__setattr__(self, "allowed_functions", functions)
        object.__setattr__(self, "sensitive_columns", sensitive)
        object.__setattr__(
            self,
            "max_rows",
            _positive_int(self.max_rows, field="max_rows", upper=1_000_000),
        )
        object.__setattr__(
            self,
            "max_tables",
            _positive_int(self.max_tables, field="max_tables", upper=1_000),
        )
        object.__setattr__(
            self,
            "max_ast_nodes",
            _positive_int(
                self.max_ast_nodes,
                field="max_ast_nodes",
                upper=1_000_000,
            ),
        )

    def allows_relation(self, schema: str, table: str) -> bool:
        return schema in self.allowed_schemas and (
            f"{schema}.{table}" in self.allowed_tables
            or f"{schema}.*" in self.allowed_tables
        )

    def is_sensitive(self, schema: str, table: str, column: str) -> bool:
        return f"{schema}.{table}.{column}" in self.sensitive_columns

    @property
    def fingerprint(self) -> str:
        return _canonical_fingerprint(
            {
                "default_schema": self.default_schema,
                "allowed_schemas": sorted(self.allowed_schemas),
                "allowed_tables": sorted(self.allowed_tables),
                "allowed_functions": sorted(self.allowed_functions),
                "sensitive_columns": sorted(self.sensitive_columns),
                "max_rows": self.max_rows,
                "max_tables": self.max_tables,
                "max_ast_nodes": self.max_ast_nodes,
            }
        )


@dataclass(frozen=True, slots=True)
class SqlColumnReference:
    schema: str
    table: str
    column: str

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}.{self.column}"


@dataclass(frozen=True, slots=True)
class SqlRelationReference:
    schema: str
    table: str
    alias: str
    kind: SqlRelationKind

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True, slots=True)
class SqlProjection:
    ordinal: int
    label: str
    sensitive: bool
    source: SqlColumnReference | None = None


@dataclass(frozen=True, slots=True)
class CompiledSqlQuery:
    normalized_sql: str = field(repr=False)
    executable_sql: str = field(repr=False)
    parameters: tuple[object, ...] = field(repr=False)
    relations: tuple[SqlRelationReference, ...]
    projections: tuple[SqlProjection, ...]
    masked_ordinals: tuple[int, ...]
    max_rows: int
    limit_applied: int
    statement_fingerprint: str = field(repr=False)
    shape_fingerprint: str
    catalog_fingerprint: str
    policy_fingerprint: str

    def __post_init__(self) -> None:
        if self.parameters:
            raise ValueError("compiled read-only SQL cannot contain parameters")
        if self.limit_applied != self.max_rows + 1:
            raise ValueError("limit_applied must equal max_rows + 1")
        expected = tuple(sorted(set(self.masked_ordinals)))
        if expected != self.masked_ordinals:
            raise ValueError("masked_ordinals must be sorted and unique")
        if any(index < 0 or index >= len(self.projections) for index in expected):
            raise ValueError("masked_ordinals must reference an output projection")
        for value in (
            self.statement_fingerprint,
            self.shape_fingerprint,
            self.catalog_fingerprint,
            self.policy_fingerprint,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("SQL fingerprints must be lowercase SHA-256 values")


__all__ = [
    "CompiledSqlQuery",
    "SqlCatalog",
    "SqlCatalogColumn",
    "SqlCatalogRelation",
    "SqlColumnReference",
    "SqlPolicy",
    "SqlPolicyCode",
    "SqlPolicyError",
    "SqlProjection",
    "SqlRelationKind",
    "SqlRelationReference",
]
