"""add persistent Model Profiles and role Assignments

Revision ID: 0014_model_control_plane
Revises: 0013_canonical_thread_schema
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op


revision = "0014_model_control_plane"
down_revision = "0013_canonical_thread_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=160), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("timeout_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("supports_stream", sa.Boolean(), nullable=False),
        sa.Column("supports_structured_output", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "timeout_seconds > 0 AND timeout_seconds <= 600",
            name="ck_model_profile_timeout_range",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_model_profile_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "display_name",
            name="uq_model_profile_display_name",
        ),
    )
    op.create_index(
        "ix_model_profiles_created_by_user_id",
        "model_profiles",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_model_profiles_provider",
        "model_profiles",
        ["provider"],
    )
    op.create_index(
        "ix_model_profiles_source",
        "model_profiles",
        ["source"],
    )

    op.create_table(
        "model_assignments",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["model_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("role"),
    )
    op.create_index(
        "ix_model_assignments_profile_id",
        "model_assignments",
        ["profile_id"],
    )
    op.create_index(
        "ix_model_assignments_updated_by_user_id",
        "model_assignments",
        ["updated_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_assignments_updated_by_user_id",
        table_name="model_assignments",
    )
    op.drop_index(
        "ix_model_assignments_profile_id",
        table_name="model_assignments",
    )
    op.drop_table("model_assignments")
    op.drop_index(
        "ix_model_profiles_source",
        table_name="model_profiles",
    )
    op.drop_index(
        "ix_model_profiles_provider",
        table_name="model_profiles",
    )
    op.drop_index(
        "ix_model_profiles_created_by_user_id",
        table_name="model_profiles",
    )
    op.drop_table("model_profiles")
