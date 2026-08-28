"""append-only message journal idempotency key

Revision ID: 0003_messages
Revises: 0002_runtime
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_messages"
down_revision = "0002_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("client_message_id", sa.String(length=128), nullable=True),
    )
    with op.batch_alter_table("chat_messages") as batch:
        batch.create_unique_constraint(
            "uq_chat_message_client_id", ["session_ref_id", "client_message_id"]
        )
    op.create_index(
        "ix_chat_messages_client_message_id",
        "chat_messages",
        ["client_message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_client_message_id", table_name="chat_messages")
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_constraint("uq_chat_message_client_id", type_="unique")
        batch.drop_column("client_message_id")
