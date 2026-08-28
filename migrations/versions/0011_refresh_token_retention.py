"""index refresh-token ledger retention scans

Revision ID: 0011_refresh_token_retention
Revises: 0010_guardrails_and_sandbox
Create Date: 2026-07-17
"""

from alembic import op


revision = "0011_refresh_token_retention"
down_revision = "0010_guardrails_and_sandbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_refresh_tokens_expires_at",
        "refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_refresh_tokens_expires_at",
        table_name="refresh_tokens",
    )
