"""add persistent Skill and custom Tool control plane

Revision ID: 0018_capability_control_plane
Revises: 0017_rag_eval_provider_stage
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op


revision = "0018_capability_control_plane"
down_revision = "0017_rag_eval_provider_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_state",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("web_research_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "capability_skill_profiles",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.JSON(), nullable=False),
        sa.Column("required_roles_json", sa.JSON(), nullable=False),
        sa.Column("required_secrets_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('builtin', 'custom')",
            name="ck_capability_skill_source",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_index(
        "ix_capability_skill_profiles_created_by_user_id",
        "capability_skill_profiles",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_capability_skill_profiles_source",
        "capability_skill_profiles",
        ["source"],
    )

    op.create_table(
        "capability_http_tool_profiles",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("group", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=2048), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("static_headers_json", sa.JSON(), nullable=False),
        sa.Column("secret_headers_json", sa.JSON(), nullable=False),
        sa.Column("required_roles_json", sa.JSON(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("idempotent", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("max_response_bytes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "method IN ('GET', 'POST')",
            name="ck_capability_http_tool_method",
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0 AND timeout_seconds <= 120",
            name="ck_capability_http_tool_timeout",
        ),
        sa.CheckConstraint(
            "max_response_bytes >= 1024 AND max_response_bytes <= 8388608",
            name="ck_capability_http_tool_response_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_index(
        "ix_capability_http_tool_profiles_created_by_user_id",
        "capability_http_tool_profiles",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_capability_http_tool_profiles_group",
        "capability_http_tool_profiles",
        ["group"],
    )

    op.create_table(
        "sql_assistant_profiles",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("dsn_secret_name", sa.String(length=128), nullable=False),
        sa.Column("expected_role", sa.String(length=63), nullable=False),
        sa.Column("allowed_schemas_json", sa.JSON(), nullable=False),
        sa.Column("allowed_tables_json", sa.JSON(), nullable=False),
        sa.Column("sensitive_columns_json", sa.JSON(), nullable=False),
        sa.Column("statement_timeout_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("max_rows", sa.Integer(), nullable=False),
        sa.Column("max_result_bytes", sa.Integer(), nullable=False),
        sa.Column("max_estimated_cost", sa.Numeric(18, 3), nullable=False),
        sa.Column("max_estimated_rows", sa.Integer(), nullable=False),
        sa.Column("max_estimated_bytes", sa.Integer(), nullable=False),
        sa.Column("catalog_cache_ttl_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "statement_timeout_seconds > 0 AND statement_timeout_seconds <= 120",
            name="ck_sql_assistant_profile_statement_timeout",
        ),
        sa.CheckConstraint(
            "max_rows >= 1 AND max_rows <= 10000",
            name="ck_sql_assistant_profile_max_rows",
        ),
        sa.CheckConstraint(
            "max_result_bytes >= 1024 AND max_result_bytes <= 16777216",
            name="ck_sql_assistant_profile_result_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sql_assistant_profiles_updated_by_user_id",
        "sql_assistant_profiles",
        ["updated_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sql_assistant_profiles_updated_by_user_id",
        table_name="sql_assistant_profiles",
    )
    op.drop_table("sql_assistant_profiles")
    op.drop_index(
        "ix_capability_http_tool_profiles_group",
        table_name="capability_http_tool_profiles",
    )
    op.drop_index(
        "ix_capability_http_tool_profiles_created_by_user_id",
        table_name="capability_http_tool_profiles",
    )
    op.drop_table("capability_http_tool_profiles")
    op.drop_index(
        "ix_capability_skill_profiles_source",
        table_name="capability_skill_profiles",
    )
    op.drop_index(
        "ix_capability_skill_profiles_created_by_user_id",
        table_name="capability_skill_profiles",
    )
    op.drop_table("capability_skill_profiles")
    op.drop_table("capability_state")
