"""LangGraph PostgreSQL checkpoint tables and HITL resume metadata

Revision ID: 0006_native_checkpoints
Revises: 0005_event_journal
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_native_checkpoints"
down_revision = "0005_event_journal"
branch_labels = None
depends_on = None


JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BINARY = sa.LargeBinary().with_variant(postgresql.BYTEA(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "checkpoint_migrations",
        sa.Column("v", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("v"),
    )
    op.bulk_insert(
        sa.table("checkpoint_migrations", sa.column("v", sa.Integer())),
        [{"v": 9}],
    )
    op.create_table(
        "checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("checkpoint", JSON_DOCUMENT, nullable=False),
        sa.Column("metadata", JSON_DOCUMENT, server_default="{}", nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
    )
    op.create_index("checkpoints_thread_id_idx", "checkpoints", ["thread_id"])
    op.create_table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("blob", BINARY, nullable=True),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "channel", "version"),
    )
    op.create_index("checkpoint_blobs_thread_id_idx", "checkpoint_blobs", ["thread_id"])
    op.create_table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("blob", BINARY, nullable=False),
        sa.Column("task_path", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint(
            "thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"
        ),
    )
    op.create_index(
        "checkpoint_writes_thread_id_idx", "checkpoint_writes", ["thread_id"]
    )

    op.add_column(
        "run_checkpoints",
        sa.Column("interrupt_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "run_checkpoints",
        sa.Column("resume_idempotency_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "run_checkpoints",
        sa.Column("resume_payload_json", sa.JSON(), nullable=True),
    )
    with op.batch_alter_table("run_checkpoints") as batch:
        batch.create_unique_constraint(
            "uq_run_checkpoint_resume_idempotency",
            ["run_id", "resume_idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("run_checkpoints") as batch:
        batch.drop_constraint("uq_run_checkpoint_resume_idempotency", type_="unique")
        batch.drop_column("resume_payload_json")
        batch.drop_column("resume_idempotency_key")
        batch.drop_column("interrupt_id")

    op.drop_index("checkpoint_writes_thread_id_idx", table_name="checkpoint_writes")
    op.drop_table("checkpoint_writes")
    op.drop_index("checkpoint_blobs_thread_id_idx", table_name="checkpoint_blobs")
    op.drop_table("checkpoint_blobs")
    op.drop_index("checkpoints_thread_id_idx", table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_table("checkpoint_migrations")
