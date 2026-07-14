"""durable run event sequence

Revision ID: 0005_event_journal
Revises: 0004_run_reservations
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_event_journal"
down_revision = "0004_run_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "last_event_sequence", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.execute(
        """
        UPDATE runs
        SET last_event_sequence = COALESCE((
            SELECT MAX(sequence) FROM run_events WHERE run_events.run_id = runs.id
        ), 0)
        """
    )


def downgrade() -> None:
    op.drop_column("runs", "last_event_sequence")
