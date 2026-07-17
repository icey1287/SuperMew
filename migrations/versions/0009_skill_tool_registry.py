"""pin immutable Skill snapshots to durable Runs

Revision ID: 0009_skill_tool_registry
Revises: 0008_indexing_worker
Create Date: 2026-07-16
"""

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "0009_skill_tool_registry"
down_revision = "0008_indexing_worker"
branch_labels = None
depends_on = None


def _backfill_tool_audit_keys(connection) -> None:
    audits = sa.table(
        "tool_audits",
        sa.column("id", sa.Integer()),
        sa.column("audit_key", sa.String()),
    )
    for audit_id in connection.execute(sa.select(audits.c.id)).scalars():
        digest = hashlib.sha256(f"legacy-tool-audit:{audit_id}".encode()).hexdigest()
        connection.execute(
            audits.update().where(audits.c.id == audit_id).values(audit_key=digest)
        )


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("skill_name", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("skill_version", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("skill_content_hash", sa.CHAR(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("skill_activation_source", sa.String(length=32), nullable=True)
        )
        batch.create_check_constraint(
            "ck_runs_skill_snapshot_complete",
            "(skill_name IS NULL AND skill_version IS NULL "
            "AND skill_content_hash IS NULL AND skill_activation_source IS NULL) "
            "OR (skill_name IS NOT NULL AND skill_version IS NOT NULL "
            "AND skill_content_hash IS NOT NULL "
            "AND skill_activation_source IS NOT NULL)",
        )

    with op.batch_alter_table("tool_audits") as batch:
        batch.add_column(
            sa.Column("tool_call_id", sa.String(length=128), nullable=True)
        )
        batch.add_column(sa.Column("audit_key", sa.CHAR(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "tool_version",
                sa.String(length=64),
                server_default="",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "skill_name",
                sa.String(length=64),
                server_default="",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("result_size", sa.Integer(), server_default="0", nullable=False)
        )
        batch.create_index("ix_tool_audits_tool_call_id", ["tool_call_id"])

    _backfill_tool_audit_keys(op.get_bind())

    with op.batch_alter_table("tool_audits") as batch:
        batch.alter_column(
            "audit_key", existing_type=sa.CHAR(length=64), nullable=False
        )
        batch.create_unique_constraint(
            "uq_tool_audit_run_key",
            ["run_id", "audit_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_audits") as batch:
        batch.drop_index("ix_tool_audits_tool_call_id")
        batch.drop_constraint("uq_tool_audit_run_key", type_="unique")
        batch.drop_column("result_size")
        batch.drop_column("skill_name")
        batch.drop_column("tool_version")
        batch.drop_column("audit_key")
        batch.drop_column("tool_call_id")

    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_runs_skill_snapshot_complete", type_="check")
        batch.drop_column("skill_activation_source")
        batch.drop_column("skill_content_hash")
        batch.drop_column("skill_version")
        batch.drop_column("skill_name")
