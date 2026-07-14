"""run reservation, fencing and message links

Revision ID: 0004_run_reservations
Revises: 0003_messages
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_run_reservations"
down_revision = "0003_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("fencing_token", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("runs", sa.Column("user_message_id", sa.Integer(), nullable=True))
    op.add_column(
        "runs", sa.Column("assistant_message_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "runs", sa.Column("supersedes_run_id", sa.String(length=64), nullable=True)
    )
    with op.batch_alter_table("runs") as batch:
        batch.create_unique_constraint(
            "uq_run_assistant_message", ["assistant_message_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("uq_run_assistant_message", type_="unique")
        batch.drop_column("supersedes_run_id")
        batch.drop_column("assistant_message_id")
        batch.drop_column("user_message_id")
        batch.drop_column("fencing_token")
