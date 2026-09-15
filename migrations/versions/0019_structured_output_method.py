"""configure the structured output protocol on Model Profiles

Revision ID: 0019_structured_output_method
Revises: 0018_capability_control_plane
"""

import sqlalchemy as sa
from alembic import op


revision = "0019_structured_output_method"
down_revision = "0018_capability_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "structured_output_method",
                sa.String(32),
                nullable=False,
                server_default="json_schema",
            )
        )
        batch_op.create_check_constraint(
            "ck_model_profile_structured_output_method",
            "structured_output_method IN ('json_schema', 'function_calling')",
        )


def downgrade() -> None:
    # Function Calling may already be frozen in Run, Checkpoint or Evaluation
    # snapshots. Dropping the setting would make the old runtime misinterpret it.
    raise RuntimeError("Structured output protocol migration is forward-only")
