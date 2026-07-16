from __future__ import annotations

import os
import secrets

import psycopg
import pytest
from psycopg import sql as pg_sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from backend.core.settings import SqlAssistantSettings
from backend.sql_assistant.postgres import SqlAssistantError
from backend.sql_assistant.runtime import SqlAssistantRuntime


TEST_DSN = os.getenv("TEST_SQL_ASSISTANT_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="TEST_SQL_ASSISTANT_DSN is not configured",
)


def _role_dsn(admin_dsn: str, *, role: str, password: str) -> str:
    parameters = conninfo_to_dict(admin_dsn)
    parameters["user"] = role
    parameters["password"] = password
    return make_conninfo(**parameters)


def _cleanup_fixture(
    *,
    schema: str,
    role: str,
    database: str | None,
    schema_created: bool,
    role_created: bool,
    restore_public_temp: bool,
    restore_public_create: bool,
) -> None:
    try:
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            with admin.cursor() as cursor:

                def attempt(query) -> None:
                    try:
                        cursor.execute(query)
                    except psycopg.Error:
                        pass

                if restore_public_temp and database:
                    attempt(
                        pg_sql.SQL("GRANT TEMPORARY ON DATABASE {} TO PUBLIC").format(
                            pg_sql.Identifier(database)
                        )
                    )
                if restore_public_create:
                    attempt("GRANT CREATE ON SCHEMA public TO PUBLIC")
                if schema_created:
                    attempt(
                        pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                            pg_sql.Identifier(schema)
                        )
                    )
                if role_created:
                    attempt(
                        pg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                            pg_sql.Identifier(role)
                        )
                    )
    except psycopg.Error:
        pass


def test_postgres_adapter_enforces_read_only_plan_mask_and_result_limits() -> None:
    suffix = secrets.token_hex(6)
    schema = f"sql_assistant_{suffix}"
    role = f"sql_reader_{suffix}"
    password = secrets.token_urlsafe(24)
    runtime: SqlAssistantRuntime | None = None
    public_had_temp = False
    public_had_create = False
    database: str | None = None
    role_created = False
    schema_created = False
    temp_revoked = False
    public_create_revoked = False
    provisioning_failed = False

    try:
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                database = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_database AS database,
                               LATERAL pg_catalog.aclexplode(
                                   COALESCE(
                                       database.datacl,
                                       pg_catalog.acldefault(
                                           'd',
                                           database.datdba
                                       )
                                   )
                               ) AS privilege
                         WHERE database.datname = current_database()
                           AND privilege.grantee = 0
                           AND privilege.privilege_type = 'TEMPORARY'
                    )
                    """
                )
                public_had_temp = bool(cursor.fetchone()[0])
                cursor.execute(
                    pg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        pg_sql.Identifier(role),
                        pg_sql.Literal(password),
                    )
                )
                role_created = True
                cursor.execute(
                    "SELECT pg_catalog.has_schema_privilege(%s, 'public', 'CREATE')",
                    (role,),
                )
                public_had_create = bool(cursor.fetchone()[0])
                if public_had_create:
                    cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                    public_create_revoked = True
                cursor.execute(
                    pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema))
                )
                schema_created = True
                cursor.execute(
                    pg_sql.SQL(
                        "CREATE TABLE {}.accounts "
                        "(id bigint PRIMARY KEY, email text NOT NULL)"
                    ).format(pg_sql.Identifier(schema))
                )
                cursor.execute(
                    pg_sql.SQL(
                        "INSERT INTO {}.accounts (id, email) VALUES "
                        "(1, 'first@example.com'), "
                        "(2, 'second@example.com'), "
                        "(3, 'third@example.com')"
                    ).format(pg_sql.Identifier(schema))
                )
                cursor.execute(
                    pg_sql.SQL(
                        "ALTER TABLE {}.accounts ENABLE ROW LEVEL SECURITY"
                    ).format(pg_sql.Identifier(schema))
                )
                cursor.execute(
                    pg_sql.SQL(
                        "CREATE POLICY sql_assistant_reader ON {}.accounts "
                        "FOR SELECT TO {} USING (true)"
                    ).format(
                        pg_sql.Identifier(schema),
                        pg_sql.Identifier(role),
                    )
                )
                cursor.execute(
                    pg_sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                        pg_sql.Identifier(database)
                    )
                )
                temp_revoked = True
                cursor.execute(
                    pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        pg_sql.Identifier(schema),
                        pg_sql.Identifier(role),
                    )
                )
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT ON {}.accounts TO {}").format(
                        pg_sql.Identifier(schema),
                        pg_sql.Identifier(role),
                    )
                )
    except psycopg.Error:
        provisioning_failed = True

    try:
        if provisioning_failed:
            pytest.skip("integration database cannot provision an isolated reader role")
        settings = SqlAssistantSettings(
            enabled=True,
            dsn=SecretStr(_role_dsn(TEST_DSN, role=role, password=password)),
            expected_role=role,
            allowed_schemas_raw=schema,
            allowed_tables_raw=f"{schema}.accounts",
            sensitive_columns_raw=f"{schema}.accounts.email",
            max_rows=2,
            max_result_bytes=4096,
            max_cell_bytes=256,
            max_estimated_cost=1_000_000,
            max_estimated_rows=1_000_000,
            max_estimated_bytes=64 * 1024 * 1024,
            pool_min_size=1,
            pool_max_size=1,
            strict_privilege_check=True,
        )
        runtime = SqlAssistantRuntime(settings=settings)

        schema_result = runtime.describe_schema(())
        query_result = runtime.query(
            f'SELECT id, email FROM "{schema}".accounts ORDER BY id',
            deadline_at=None,
            cancellation_probe=None,
        )

        assert schema_result["observability_metadata"] == {
            "schema_count": 1,
            "table_count": 1,
            "column_count": 2,
            "catalog_cache_hit": True,
        }
        assert query_result["columns"] == ["id", "email"]
        assert query_result["rows"] == [[1, "***"], [2, "***"]]
        assert query_result["row_count"] == 2
        assert query_result["truncated"] is True

        with pytest.raises(SqlAssistantError) as denied:
            runtime.query(
                f"UPDATE \"{schema}\".accounts SET email = 'leak@example.com'",
                deadline_at=None,
                cancellation_probe=None,
            )
        assert denied.value.code == "SQL_POLICY_DENIED"
        assert "leak@example.com" not in str(denied.value)
    finally:
        if runtime is not None:
            runtime.close()
        _cleanup_fixture(
            schema=schema,
            role=role,
            database=database,
            schema_created=schema_created,
            role_created=role_created,
            restore_public_temp=public_had_temp and temp_revoked,
            restore_public_create=public_had_create and public_create_revoked,
        )
