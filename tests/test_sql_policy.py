from __future__ import annotations

import re

import pytest
import sqlglot
from sqlglot import exp

from backend.sql_assistant import (
    SqlCatalog,
    SqlCatalogColumn,
    SqlCatalogRelation,
    SqlPolicy,
    SqlPolicyCode,
    SqlPolicyCompiler,
    SqlPolicyError,
    SqlRelationKind,
)


def _catalog(*, revision: str = "catalog-v1") -> SqlCatalog:
    return SqlCatalog(
        database="analytics",
        revision=revision,
        relations=(
            SqlCatalogRelation(
                schema="public",
                name="users",
                owner="data_owner",
                columns=(
                    SqlCatalogColumn("id", "bigint", nullable=False),
                    SqlCatalogColumn("name", "text"),
                    SqlCatalogColumn("email", "text", sensitive=True),
                    SqlCatalogColumn("phone", "text"),
                    SqlCatalogColumn("created_at", "timestamptz"),
                ),
            ),
            SqlCatalogRelation(
                schema="public",
                name="orders",
                owner="data_owner",
                columns=(
                    SqlCatalogColumn("id", "bigint", nullable=False),
                    SqlCatalogColumn("user_id", "bigint", nullable=False),
                    SqlCatalogColumn("email", "text", sensitive=True),
                    SqlCatalogColumn("total", "numeric"),
                ),
            ),
            SqlCatalogRelation(
                schema="analytics",
                name="daily_events",
                kind=SqlRelationKind.VIEW,
                owner="reporting_owner",
                columns=(
                    SqlCatalogColumn("day", "date", nullable=False),
                    SqlCatalogColumn("event_count", "bigint", nullable=False),
                ),
            ),
        ),
    )


def _policy(**overrides) -> SqlPolicy:
    values = {
        "default_schema": "public",
        "allowed_schemas": frozenset({"public", "analytics"}),
        "allowed_tables": frozenset({"public.users", "public.orders", "analytics.*"}),
        "allowed_functions": frozenset(
            {
                "avg",
                "cast",
                "coalesce",
                "count",
                "date_trunc",
                "lower",
                "max",
                "min",
                "sum",
            }
        ),
        "sensitive_columns": frozenset({"public.users.phone"}),
        "max_rows": 100,
        "max_tables": 8,
        "max_ast_nodes": 1_000,
    }
    values.update(overrides)
    return SqlPolicy(**values)


def _compiler(**policy_overrides) -> SqlPolicyCompiler:
    return SqlPolicyCompiler(_policy(**policy_overrides))


def _assert_denied(query: str, code: SqlPolicyCode, **policy_overrides) -> None:
    with pytest.raises(SqlPolicyError) as caught:
        _compiler(**policy_overrides).compile(query, _catalog())
    assert caught.value.code is code
    assert query not in str(caught.value)


def test_compiler_normalizes_default_schema_round_trips_and_applies_outer_limit():
    compiled = _compiler().compile(
        """
        select id, count(*) as order_count
          from users
         group by id
         order by id
         limit 5
        """,
        _catalog(),
    )

    assert 'FROM "public"."users" AS "users"' in compiled.normalized_sql
    assert compiled.normalized_sql.endswith("LIMIT 5")
    assert compiled.executable_sql.startswith("SELECT * FROM (")
    assert compiled.executable_sql.endswith("LIMIT 101")
    assert compiled.max_rows == 100
    assert compiled.limit_applied == 101
    assert compiled.parameters == ()
    assert [item.qualified_name for item in compiled.relations] == ["public.users"]
    assert [item.label for item in compiled.projections] == ["id", "order_count"]
    assert compiled.masked_ordinals == ()

    reparsed = sqlglot.parse(compiled.executable_sql, read="postgres")
    assert len(reparsed) == 1
    assert isinstance(reparsed[0], exp.Query)
    assert isinstance(reparsed[0].args.get("limit"), exp.Limit)


@pytest.mark.parametrize("operator", ["union", "intersect", "except"])
def test_set_operations_receive_one_unbypassable_outer_limit(operator):
    compiled = _compiler(max_rows=7).compile(
        f"select id from users {operator} select id from orders",
        _catalog(),
    )

    assert compiled.limit_applied == 8
    assert compiled.executable_sql.startswith("SELECT * FROM (")
    assert compiled.executable_sql.endswith("LIMIT 8")
    parsed = sqlglot.parse_one(compiled.executable_sql, read="postgres")
    assert isinstance(parsed, exp.Select)
    assert isinstance(parsed.args.get("limit"), exp.Limit)
    assert isinstance(parsed.args["from_"].this.this, exp.SetOperation)


def test_cte_names_shadow_physical_relations_without_expanding_the_allowlist():
    compiled = _compiler().compile(
        "with users as (select id from orders) select id from users",
        _catalog(),
    )

    assert [relation.qualified_name for relation in compiled.relations] == [
        "public.orders"
    ]
    assert 'FROM "users" AS "users"' in compiled.normalized_sql


def test_quoted_canonical_identifiers_work_but_case_cannot_bypass_the_catalog():
    compiled = _compiler().compile(
        'select "users"."email" as "Contact" from "public"."users" as "users"',
        _catalog(),
    )
    assert compiled.masked_ordinals == (0,)
    assert compiled.projections[0].label == "Contact"

    _assert_denied(
        'select "ID" from "PUBLIC"."USERS"',
        SqlPolicyCode.RELATION_DENIED,
    )


def test_schema_qualified_custom_operator_cannot_bypass_function_policy():
    _assert_denied(
        "select id OPERATOR(public.===) 1 from users",
        SqlPolicyCode.OPERATOR_DENIED,
    )


def test_statement_and_shape_fingerprints_are_canonical_and_literal_aware():
    compiler = _compiler()
    first = compiler.compile("select id from users where id = 1", _catalog())
    formatted = compiler.compile(
        " SELECT ID FROM USERS WHERE ID=1 ",
        _catalog(),
    )
    other_literal = compiler.compile(
        "select id from users where id = 999",
        _catalog(),
    )

    assert first.statement_fingerprint == formatted.statement_fingerprint
    assert first.statement_fingerprint != other_literal.statement_fingerprint
    assert first.shape_fingerprint == other_literal.shape_fingerprint

    literal_projection_a = compiler.compile("select 1", _catalog())
    literal_projection_b = compiler.compile("select 999", _catalog())
    assert (
        literal_projection_a.shape_fingerprint == literal_projection_b.shape_fingerprint
    )
    for fingerprint in (
        first.statement_fingerprint,
        first.shape_fingerprint,
        first.catalog_fingerprint,
        first.policy_fingerprint,
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)


def test_catalog_and_policy_fingerprints_pin_the_authorization_snapshot():
    query = "select id from users"
    first = _compiler().compile(query, _catalog(revision="v1"))
    changed_catalog = _compiler().compile(query, _catalog(revision="v2"))
    changed_policy = _compiler(max_rows=50).compile(query, _catalog(revision="v1"))

    assert first.catalog_fingerprint != changed_catalog.catalog_fingerprint
    assert first.policy_fingerprint != changed_policy.policy_fingerprint
    assert first.statement_fingerprint != changed_policy.statement_fingerprint


def test_compiled_query_repr_never_contains_sql_literals_or_statement_hash():
    compiled = _compiler().compile(
        "select id from users where name = 'private-customer-name'",
        _catalog(),
    )

    rendered = repr(compiled)
    assert "private-customer-name" not in rendered
    assert compiled.normalized_sql not in rendered
    assert compiled.executable_sql not in rendered
    assert compiled.statement_fingerprint not in rendered


def test_sensitive_columns_are_marked_by_zero_based_top_level_projection_ordinal():
    compiled = _compiler().compile(
        "select id, email as contact, phone from users order by id",
        _catalog(),
    )

    assert compiled.masked_ordinals == (1, 2)
    assert [projection.sensitive for projection in compiled.projections] == [
        False,
        True,
        True,
    ]
    assert compiled.projections[1].source is not None
    assert compiled.projections[1].source.qualified_name == "public.users.email"


@pytest.mark.parametrize(
    "query",
    [
        "select lower(email) from users",
        "select id from users where email = 'person@example.com'",
        ("select u.id from users u join orders o on u.email = o.email"),
        "select email from users group by email",
        "select email as contact from users order by contact",
        "select email from users order by 1",
        "select distinct email from users",
        "select (select email from users limit 1) as nested_email",
        "with contacts as (select email from users) select email from contacts",
        "select email from users union all select email from users",
        "select users.email from users natural join orders",
    ],
)
def test_sensitive_columns_cannot_affect_expressions_or_query_semantics(query):
    _assert_denied(query, SqlPolicyCode.SENSITIVE_USAGE_DENIED)


@pytest.mark.parametrize(
    "query",
    [
        "delete from users",
        "update users set name = 'changed'",
        "insert into users (id) values (1)",
        "create table unsafe (id bigint)",
        "drop table users",
        "copy users to stdout",
        "call unsafe_procedure()",
        "set statement_timeout = 0",
        "begin",
        "select id into temporary copied_users from users",
        "select id from users for update",
        ("with removed as (delete from users returning id) select id from removed"),
    ],
)
def test_only_read_only_query_asts_are_allowed(query):
    _assert_denied(query, SqlPolicyCode.STATEMENT_NOT_ALLOWED)


def test_multiple_statements_are_rejected_before_authorization():
    _assert_denied(
        "select id from users; select id from orders",
        SqlPolicyCode.MULTIPLE_STATEMENTS,
    )


@pytest.mark.parametrize(
    "query",
    [
        "select id from users where id = $1",
        "select id from users where id = :id",
        "select id from users where id = ?",
    ],
)
def test_placeholders_are_rejected(query):
    _assert_denied(query, SqlPolicyCode.PLACEHOLDER_DENIED)


@pytest.mark.parametrize(
    "query",
    [
        "select * from users",
        "select users.* from users",
        "select users from users",
        "select row(users) from users",
        "select count(users.*) from users",
        "select exists(select * from orders)",
    ],
)
def test_user_wildcard_projections_are_rejected(query):
    _assert_denied(query, SqlPolicyCode.WILDCARD_DENIED)


def test_count_star_is_the_only_allowed_star_form():
    compiled = _compiler().compile("select count(*) as total from users", _catalog())
    assert compiled.projections[0].label == "total"
    assert "COUNT(*)" in compiled.normalized_sql


def test_output_alias_matching_a_table_name_is_not_mistaken_for_a_whole_row():
    compiled = _compiler().compile(
        "select id as users from users order by users",
        _catalog(),
    )
    assert compiled.projections[0].label == "users"


def test_function_and_table_function_allowlists_fail_closed():
    allowed = _compiler().compile("select lower(name) from users", _catalog())
    assert "LOWER" in allowed.normalized_sql
    date_trunc = _compiler().compile(
        "select date_trunc('day', created_at) from users",
        _catalog(),
    )
    assert "DATE_TRUNC" in date_trunc.normalized_sql

    _assert_denied(
        "select pg_sleep(1)",
        SqlPolicyCode.FUNCTION_DENIED,
    )
    _assert_denied(
        "select id from generate_series(1, 3) as generated(id)",
        SqlPolicyCode.TABLE_FUNCTION_DENIED,
    )
    _assert_denied(
        "select id from unnest(array[1, 2]) as generated(id)",
        SqlPolicyCode.TABLE_FUNCTION_DENIED,
    )


def test_schema_and_relation_allowlists_apply_after_default_schema_normalization():
    normalized = _compiler().compile("select id from users", _catalog())
    assert normalized.relations[0].schema == "public"

    _assert_denied(
        "select id from private.users",
        SqlPolicyCode.RELATION_DENIED,
    )
    _assert_denied(
        "select id from public.missing",
        SqlPolicyCode.RELATION_DENIED,
    )
    _assert_denied(
        "select id from analytics.daily_events",
        SqlPolicyCode.COLUMN_DENIED,
    )


def test_unknown_and_ambiguous_columns_are_rejected():
    _assert_denied(
        "select missing_column from users",
        SqlPolicyCode.COLUMN_DENIED,
    )
    _assert_denied(
        "select id from users join orders on users.id = orders.user_id",
        SqlPolicyCode.COLUMN_DENIED,
    )


def test_relation_occurrences_and_ast_nodes_have_hard_limits():
    _assert_denied(
        "select left_user.id from users left_user "
        "join users right_user on left_user.id = right_user.id",
        SqlPolicyCode.TABLE_LIMIT_EXCEEDED,
        max_tables=1,
    )
    _assert_denied(
        "select id from users where "
        + " or ".join(f"id = {index}" for index in range(20)),
        SqlPolicyCode.AST_LIMIT_EXCEEDED,
        max_ast_nodes=50,
    )


def test_policy_collections_are_frozen_and_empty_relation_rules_fail_closed():
    policy = SqlPolicy(
        allowed_schemas=["public"],
        allowed_tables=["public.users"],
        allowed_functions=["COUNT"],
        sensitive_columns=["public.users.email"],
    )
    assert policy.allowed_schemas == frozenset({"public"})
    assert policy.allowed_functions == frozenset({"count"})

    with pytest.raises(SqlPolicyError) as caught:
        SqlPolicyCompiler(
            SqlPolicy(
                allowed_schemas={"public"},
                allowed_tables=frozenset(),
            )
        ).compile("select id from users", _catalog())
    assert caught.value.code is SqlPolicyCode.RELATION_DENIED


def test_catalog_contract_rejects_duplicate_relations_and_columns():
    column = SqlCatalogColumn("id")
    with pytest.raises(ValueError, match="column names"):
        SqlCatalogRelation("public", "users", (column, column))

    relation = SqlCatalogRelation("public", "users", (column,))
    with pytest.raises(ValueError, match="relation identities"):
        SqlCatalog((relation, relation))
