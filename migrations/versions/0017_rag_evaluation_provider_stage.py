"""record logical provider failure stage for RAG evaluation cases

Revision ID: 0017_rag_eval_provider_stage
Revises: 0016_rag_evaluation_runtime
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op


revision = "0017_rag_eval_provider_stage"
down_revision = "0016_rag_evaluation_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rag_evaluation_cases",
        sa.Column("provider_error_stage", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rag_evaluation_cases", "provider_error_stage")
