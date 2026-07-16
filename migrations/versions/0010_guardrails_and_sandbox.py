"""persist Run guardrail context and ToolAudit policy identity

Revision ID: 0010_guardrails_and_sandbox
Revises: 0009_skill_tool_registry
Create Date: 2026-07-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_guardrails_and_sandbox"
down_revision = "0009_skill_tool_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                server_default="default",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "channel",
                sa.String(length=32),
                server_default="chat",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "approved_tools_json",
                sa.JSON(),
                server_default="[]",
                nullable=False,
            )
        )
        batch.create_index("ix_runs_tenant_id", ["tenant_id"])

    with op.batch_alter_table("tool_audits") as batch:
        batch.add_column(sa.Column("reason_code", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("policy_version", sa.String(length=64), nullable=True)
        )
        batch.add_column(sa.Column("policy_hash", sa.CHAR(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tool_audits") as batch:
        batch.drop_column("policy_hash")
        batch.drop_column("policy_version")
        batch.drop_column("reason_code")

    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_tenant_id")
        batch.drop_column("approved_tools_json")
        batch.drop_column("channel")
        batch.drop_column("tenant_id")
