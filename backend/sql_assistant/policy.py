from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import Scope, traverse_scope

from backend.sql_assistant.contracts import (
    CompiledSqlQuery,
    SqlCatalog,
    SqlCatalogColumn,
    SqlCatalogRelation,
    SqlColumnReference,
    SqlPolicy,
    SqlPolicyCode,
    SqlPolicyError,
    SqlProjection,
    SqlRelationReference,
)


_DIALECT = "postgres"
_RESULT_ALIAS = "_supermew_sql_result"
DEFAULT_ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "avg",
        "ceil",
        "coalesce",
        "count",
        "current_timestamp",
        "extract",
        "floor",
        "greatest",
        "least",
        "length",
        "lower",
        "max",
        "min",
        "nullif",
        "round",
        "sum",
        "timestamp_trunc",
        "upper",
    }
)
_FORBIDDEN_NODES = (
    exp.DDL,
    exp.DML,
    exp.Command,
    exp.Commit,
    exp.Execute,
    exp.Into,
    exp.Lock,
    exp.Rollback,
    exp.Set,
    exp.Transaction,
)


@dataclass(frozen=True, slots=True)
class _PreparedQuery:
    expression: exp.Query
    relations: tuple[SqlRelationReference, ...]
    projections: tuple[SqlProjection, ...]
    masked_ordinals: tuple[int, ...]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _node_count(expression: exp.Expr) -> int:
    return sum(1 for _ in expression.walk())


def _column_source(
    scope: Scope,
    column: exp.Column,
) -> exp.Table | None:
    current: Scope | None = scope
    while current is not None:
        source = current.sources.get(column.table)
        if isinstance(source, exp.Table):
            return source
        if isinstance(source, Scope):
            return None
        current = current.parent
    return None


def _direct_projection_column(projection: exp.Expr) -> exp.Column | None:
    candidate = projection.this if isinstance(projection, exp.Alias) else projection
    return candidate if isinstance(candidate, exp.Column) else None


def _identifier_token(identifier: exp.Identifier) -> str:
    if identifier.args.get("quoted"):
        escaped = identifier.name.replace('"', '""')
        return f'"{escaped}"'
    return identifier.name.lower()


def _identifier_path(expression: exp.Expr) -> tuple[str, ...] | None:
    if isinstance(expression, exp.Identifier):
        return (_identifier_token(expression),)
    if isinstance(expression, exp.Column):
        return tuple(_identifier_token(part) for part in expression.parts)
    if isinstance(expression, exp.Dot):
        left = _identifier_path(expression.this)
        right = _identifier_path(expression.expression)
        if left is not None and right is not None:
            return (*left, *right)
    return None


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        name_expression = function.this
        if isinstance(name_expression, exp.Identifier):
            name = _identifier_token(name_expression)
        else:
            name = str(function.name).lower()
    else:
        name = function.sql_name().lower()

    parent = function.parent
    if isinstance(parent, exp.Dot) and parent.expression is function:
        prefix = _identifier_path(parent.this)
        if prefix:
            return ".".join((*prefix, name))
    return name


def _shape_sql(expression: exp.Query) -> str:
    shaped = expression.copy()
    for alias in shaped.find_all(exp.Alias):
        if alias.alias.startswith("_col_") or isinstance(
            alias.this,
            (exp.Boolean, exp.Literal),
        ):
            alias.set("alias", exp.to_identifier("_value_shape", quoted=True))

    def redact(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Boolean):
            return exp.false()
        if isinstance(node, exp.Literal):
            if node.is_string:
                return exp.Literal.string("__value__")
            return exp.Literal.number(0)
        return node

    return shaped.transform(redact).sql(dialect=_DIALECT, pretty=False)


class SqlPolicyCompiler:
    """Deep read-only SQL Module shared by tools and PostgreSQL Adapters."""

    def __init__(self, policy: SqlPolicy) -> None:
        if not isinstance(policy, SqlPolicy):
            raise TypeError("policy must be a SqlPolicy")
        self.policy = policy

    def compile(self, query: str, catalog: SqlCatalog) -> CompiledSqlQuery:
        if not isinstance(catalog, SqlCatalog):
            raise TypeError("catalog must be a SqlCatalog")

        parsed = self._parse_user_query(query)
        first = self._prepare(parsed, catalog, original_sql=query)
        normalized_sql = first.expression.sql(dialect=_DIALECT, pretty=False)

        reparsed = self._parse_generated_query(normalized_sql)
        second = self._prepare(reparsed, catalog, original_sql=normalized_sql)
        regenerated_sql = second.expression.sql(dialect=_DIALECT, pretty=False)
        if regenerated_sql != normalized_sql or (
            first.relations,
            first.projections,
            first.masked_ordinals,
        ) != (
            second.relations,
            second.projections,
            second.masked_ordinals,
        ):
            raise SqlPolicyError(
                SqlPolicyCode.NORMALIZATION_FAILED,
                "SQL normalization was not idempotent",
            )

        limit_applied = self.policy.max_rows + 1
        executable = (
            exp.select("*")
            .from_(second.expression.copy().subquery(alias=_RESULT_ALIAS))
            .limit(limit_applied)
        )
        executable_sql = executable.sql(dialect=_DIALECT, pretty=False)
        self._parse_generated_query(executable_sql)

        return CompiledSqlQuery(
            normalized_sql=normalized_sql,
            executable_sql=executable_sql,
            parameters=(),
            relations=second.relations,
            projections=second.projections,
            masked_ordinals=second.masked_ordinals,
            max_rows=self.policy.max_rows,
            limit_applied=limit_applied,
            statement_fingerprint=_sha256(executable_sql),
            shape_fingerprint=_sha256(_shape_sql(second.expression)),
            catalog_fingerprint=catalog.fingerprint,
            policy_fingerprint=self.policy.fingerprint,
        )

    def _parse_user_query(self, query: str) -> exp.Query:
        if not isinstance(query, str) or not query.strip():
            raise SqlPolicyError(SqlPolicyCode.INVALID_SQL, "SQL query is empty")
        try:
            parsed = sqlglot.parse(
                query,
                read=_DIALECT,
                error_level=ErrorLevel.RAISE,
            )
        except SqlglotError as exc:
            raise SqlPolicyError(
                SqlPolicyCode.INVALID_SQL,
                "SQL query could not be parsed",
            ) from exc

        statements = [statement for statement in parsed if statement is not None]
        if len(statements) != 1:
            code = (
                SqlPolicyCode.MULTIPLE_STATEMENTS
                if statements
                else SqlPolicyCode.INVALID_SQL
            )
            raise SqlPolicyError(code, "Exactly one SQL query is required")
        expression = statements[0]
        self._validate_structure(expression)
        return expression

    def _parse_generated_query(self, query: str) -> exp.Query:
        try:
            parsed = sqlglot.parse(
                query,
                read=_DIALECT,
                error_level=ErrorLevel.RAISE,
            )
        except SqlglotError as exc:
            raise SqlPolicyError(
                SqlPolicyCode.NORMALIZATION_FAILED,
                "Generated SQL could not be parsed",
            ) from exc
        statements = [statement for statement in parsed if statement is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise SqlPolicyError(
                SqlPolicyCode.NORMALIZATION_FAILED,
                "Generated SQL was not one query",
            )
        return statements[0]

    def _validate_structure(self, expression: exp.Expr) -> None:
        if not isinstance(expression, exp.Query):
            raise SqlPolicyError(
                SqlPolicyCode.STATEMENT_NOT_ALLOWED,
                "Only PostgreSQL query statements are allowed",
            )
        if _node_count(expression) > self.policy.max_ast_nodes:
            raise SqlPolicyError(
                SqlPolicyCode.AST_LIMIT_EXCEEDED,
                "SQL query exceeds the AST node limit",
                safe_details={"max_ast_nodes": self.policy.max_ast_nodes},
            )
        if any(isinstance(node, _FORBIDDEN_NODES) for node in expression.walk()):
            raise SqlPolicyError(
                SqlPolicyCode.STATEMENT_NOT_ALLOWED,
                "SQL query contains a forbidden statement operation",
            )
        if any(
            isinstance(node, (exp.Parameter, exp.Placeholder))
            for node in expression.walk()
        ):
            raise SqlPolicyError(
                SqlPolicyCode.PLACEHOLDER_DENIED,
                "SQL placeholders are not allowed",
            )
        if expression.find(exp.Operator) is not None:
            raise SqlPolicyError(
                SqlPolicyCode.OPERATOR_DENIED,
                "Schema-qualified custom operators are not allowed",
            )
        for table in expression.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                raise SqlPolicyError(
                    SqlPolicyCode.TABLE_FUNCTION_DENIED,
                    "Table-valued functions are not allowed",
                )
        if any(
            isinstance(node, exp.UDTF) and not isinstance(node, exp.Values)
            for node in expression.walk()
        ):
            raise SqlPolicyError(
                SqlPolicyCode.TABLE_FUNCTION_DENIED,
                "Table-valued functions are not allowed",
            )
        for star in expression.find_all(exp.Star):
            if isinstance(star.parent, exp.Count) and star.parent.this is star:
                continue
            raise SqlPolicyError(
                SqlPolicyCode.WILDCARD_DENIED,
                "Wildcard projections are not allowed",
            )

    def _prepare(
        self,
        expression: exp.Query,
        catalog: SqlCatalog,
        *,
        original_sql: str,
    ) -> _PreparedQuery:
        self._validate_structure(expression)
        try:
            normalized = normalize_identifiers(
                expression.copy(),
                dialect=_DIALECT,
                store_original_column_identifiers=True,
            )
            normalized = qualify_tables(
                normalized,
                db=self.policy.default_schema,
                dialect=_DIALECT,
            )
        except (OptimizeError, SqlglotError) as exc:
            raise SqlPolicyError(
                SqlPolicyCode.INVALID_SQL,
                "SQL table references could not be normalized",
            ) from exc

        self._validate_relations(normalized, catalog)
        try:
            qualified = qualify(
                normalized,
                dialect=_DIALECT,
                db=self.policy.default_schema,
                schema=self._mapping_schema(catalog),
                expand_alias_refs=True,
                expand_stars=False,
                infer_schema=False,
                allow_partial_qualification=False,
                validate_qualify_columns=True,
                quote_identifiers=True,
                identify=True,
                sql=original_sql,
            )
        except (OptimizeError, SqlglotError) as exc:
            raise SqlPolicyError(
                SqlPolicyCode.COLUMN_DENIED,
                "SQL columns are unknown or ambiguous",
            ) from exc

        self._validate_structure(qualified)
        relations = self._validate_relations(qualified, catalog)
        self._validate_whole_row_references(qualified)
        self._validate_functions(qualified)
        projections, masked_ordinals = self._validate_sensitive_columns(
            qualified,
            catalog,
        )
        return _PreparedQuery(
            expression=qualified,
            relations=relations,
            projections=projections,
            masked_ordinals=masked_ordinals,
        )

    @staticmethod
    def _mapping_schema(catalog: SqlCatalog) -> dict[str, dict[str, dict[str, str]]]:
        mapping: dict[str, dict[str, dict[str, str]]] = {}
        for relation in catalog.relations:
            mapping.setdefault(relation.schema, {})[relation.name] = {
                column.name: "UNKNOWN" for column in relation.columns
            }
        return mapping

    def _validate_relations(
        self,
        expression: exp.Query,
        catalog: SqlCatalog,
    ) -> tuple[SqlRelationReference, ...]:
        references: list[SqlRelationReference] = []
        seen_tables: set[int] = set()
        for scope in traverse_scope(expression):
            for source in scope.sources.values():
                if not isinstance(source, exp.Table) or id(source) in seen_tables:
                    continue
                seen_tables.add(id(source))
                if not isinstance(source.this, exp.Identifier):
                    raise SqlPolicyError(
                        SqlPolicyCode.TABLE_FUNCTION_DENIED,
                        "Table-valued functions are not allowed",
                    )
                schema = source.db
                table = source.name
                if source.catalog or not schema or not table:
                    raise SqlPolicyError(
                        SqlPolicyCode.RELATION_DENIED,
                        "SQL relation is outside the allowed catalog",
                    )
                relation = catalog.relation(schema, table)
                if relation is None or not self.policy.allows_relation(schema, table):
                    raise SqlPolicyError(
                        SqlPolicyCode.RELATION_DENIED,
                        "SQL relation is outside the allowlist",
                    )
                references.append(
                    SqlRelationReference(
                        schema=schema,
                        table=table,
                        alias=source.alias_or_name,
                        kind=relation.kind,
                    )
                )

        if len(references) > self.policy.max_tables:
            raise SqlPolicyError(
                SqlPolicyCode.TABLE_LIMIT_EXCEEDED,
                "SQL query exceeds the relation limit",
                safe_details={"max_tables": self.policy.max_tables},
            )
        return tuple(
            sorted(
                references,
                key=lambda item: (item.schema, item.table, item.alias),
            )
        )

    def _validate_functions(self, expression: exp.Query) -> None:
        for function in expression.find_all(exp.Func):
            name = _function_name(function)
            if name not in self.policy.allowed_functions:
                raise SqlPolicyError(
                    SqlPolicyCode.FUNCTION_DENIED,
                    "SQL function is outside the allowlist",
                )

    def _validate_whole_row_references(self, expression: exp.Query) -> None:
        if expression.find(exp.TableColumn) is not None:
            raise SqlPolicyError(
                SqlPolicyCode.WILDCARD_DENIED,
                "Whole-row relation references are not allowed",
            )
        for scope in traverse_scope(expression):
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            source_aliases = set(scope.sources)
            output_labels = {projection.alias_or_name for projection in select.selects}
            for column in select.find_all(exp.Column):
                if (
                    self._nearest_select(column) is not select
                    or column.table
                    or column.name not in source_aliases
                ):
                    continue
                if column.name in output_labels and self._has_ordering_ancestor(
                    column,
                    select,
                ):
                    continue
                raise SqlPolicyError(
                    SqlPolicyCode.WILDCARD_DENIED,
                    "Whole-row relation references are not allowed",
                )

    def _validate_sensitive_columns(
        self,
        expression: exp.Query,
        catalog: SqlCatalog,
    ) -> tuple[tuple[SqlProjection, ...], tuple[int, ...]]:
        scopes = list(traverse_scope(expression))
        root_scope = next(
            (scope for scope in scopes if scope.expression is expression),
            None,
        )
        root_select = expression if isinstance(expression, exp.Select) else None

        projections: list[SqlProjection] = []
        allowed_sensitive_nodes: set[int] = set()
        if root_select is not None and root_scope is not None:
            for ordinal, projection in enumerate(root_select.selects):
                source_reference: SqlColumnReference | None = None
                sensitive = False
                column = _direct_projection_column(projection)
                if column is not None:
                    table = _column_source(root_scope, column)
                    resolved = self._catalog_column(table, column, catalog)
                    if resolved is not None:
                        relation, catalog_column = resolved
                        source_reference = SqlColumnReference(
                            schema=relation.schema,
                            table=relation.name,
                            column=catalog_column.name,
                        )
                        sensitive = (
                            catalog_column.sensitive
                            or self.policy.is_sensitive(
                                relation.schema,
                                relation.name,
                                catalog_column.name,
                            )
                        )
                        if sensitive:
                            allowed_sensitive_nodes.add(id(column))
                projections.append(
                    SqlProjection(
                        ordinal=ordinal,
                        label=projection.alias_or_name or f"_col_{ordinal}",
                        sensitive=sensitive,
                        source=source_reference,
                    )
                )
        else:
            projections.extend(
                SqlProjection(
                    ordinal=ordinal,
                    label=projection.alias_or_name or f"_col_{ordinal}",
                    sensitive=False,
                )
                for ordinal, projection in enumerate(expression.selects)
            )

        seen_columns: set[int] = set()
        for scope in scopes:
            for column in scope.columns:
                if id(column) in seen_columns:
                    continue
                seen_columns.add(id(column))
                table = _column_source(scope, column)
                resolved = self._catalog_column(table, column, catalog)
                if resolved is None:
                    continue
                relation, catalog_column = resolved
                sensitive = catalog_column.sensitive or self.policy.is_sensitive(
                    relation.schema,
                    relation.name,
                    catalog_column.name,
                )
                if sensitive and id(column) not in allowed_sensitive_nodes:
                    raise SqlPolicyError(
                        SqlPolicyCode.SENSITIVE_USAGE_DENIED,
                        "Sensitive columns may only be direct top-level projections",
                    )

        masked_ordinals = tuple(
            projection.ordinal for projection in projections if projection.sensitive
        )
        if masked_ordinals and root_select is not None:
            sensitive_labels = {
                projection.label for projection in projections if projection.sensitive
            }
            for column in root_select.find_all(exp.Column):
                if (
                    not column.table
                    and column.name in sensitive_labels
                    and id(column) not in allowed_sensitive_nodes
                    and self._nearest_select(column) is root_select
                ):
                    raise SqlPolicyError(
                        SqlPolicyCode.SENSITIVE_USAGE_DENIED,
                        "Sensitive output aliases cannot affect query semantics",
                    )
            if root_select.args.get("distinct") is not None:
                raise SqlPolicyError(
                    SqlPolicyCode.SENSITIVE_USAGE_DENIED,
                    "Sensitive projections cannot use DISTINCT",
                )

        self._validate_natural_joins(scopes, catalog)
        return tuple(projections), masked_ordinals

    def _validate_natural_joins(
        self,
        scopes: list[Scope],
        catalog: SqlCatalog,
    ) -> None:
        for scope in scopes:
            joins = scope.expression.args.get("joins") or ()
            if not any(
                str(join.args.get("method") or "").upper() == "NATURAL"
                for join in joins
            ):
                continue
            relations = [
                catalog.relation(source.db, source.name)
                for source in scope.sources.values()
                if isinstance(source, exp.Table)
            ]
            present = [relation for relation in relations if relation is not None]
            counts: dict[str, int] = {}
            sensitive_names: set[str] = set()
            for relation in present:
                for column in relation.columns:
                    counts[column.name] = counts.get(column.name, 0) + 1
                    if column.sensitive or self.policy.is_sensitive(
                        relation.schema,
                        relation.name,
                        column.name,
                    ):
                        sensitive_names.add(column.name)
            if any(counts.get(name, 0) > 1 for name in sensitive_names):
                raise SqlPolicyError(
                    SqlPolicyCode.SENSITIVE_USAGE_DENIED,
                    "NATURAL JOIN cannot implicitly consume sensitive columns",
                )

    @staticmethod
    def _catalog_column(
        table: exp.Table | None,
        column: exp.Column,
        catalog: SqlCatalog,
    ) -> tuple[SqlCatalogRelation, SqlCatalogColumn] | None:
        if table is None:
            return None
        relation = catalog.relation(table.db, table.name)
        if relation is None:
            return None
        catalog_column = relation.column(column.name)
        if catalog_column is None:
            return None
        return relation, catalog_column

    @staticmethod
    def _nearest_select(node: exp.Expr) -> exp.Select | None:
        current: exp.Expr | None = node
        while current is not None:
            if isinstance(current, exp.Select):
                return current
            current = current.parent
        return None

    @staticmethod
    def _has_ordering_ancestor(node: exp.Expr, select: exp.Select) -> bool:
        current = node.parent
        while current is not None and current is not select:
            if isinstance(current, (exp.Distinct, exp.Order)):
                return True
            current = current.parent
        return False


def compile_sql(
    query: str,
    *,
    catalog: SqlCatalog,
    policy: SqlPolicy,
) -> CompiledSqlQuery:
    return SqlPolicyCompiler(policy).compile(query, catalog)


__all__ = [
    "DEFAULT_ALLOWED_FUNCTIONS",
    "SqlPolicyCompiler",
    "compile_sql",
]
