"""add persistent RAG evaluation datasets, jobs and case results

Revision ID: 0016_rag_evaluation_runtime
Revises: 0015_run_model_snapshot
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op


revision = "0016_rag_evaluation_runtime"
down_revision = "0015_run_model_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_evaluation_datasets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fingerprint",
            name="uq_rag_evaluation_dataset_fingerprint",
        ),
    )
    op.create_index(
        "ix_rag_evaluation_datasets_created_at",
        "rag_evaluation_datasets",
        ["created_at"],
    )
    op.create_index(
        "ix_rag_evaluation_datasets_created_by_user_id",
        "rag_evaluation_datasets",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_rag_evaluation_datasets_name",
        "rag_evaluation_datasets",
        ["name"],
    )

    op.create_table(
        "rag_evaluation_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("baseline_job_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_cases", sa.Integer(), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("gate_policy_json", sa.JSON(), nullable=False),
        sa.Column("model_catalog_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("model_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("owner_worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'cancelling', 'cancelled', "
            "'succeeded', 'failed')",
            name="ck_rag_evaluation_jobs_status",
        ),
        sa.CheckConstraint(
            "completed_cases >= 0 AND total_cases >= 1 "
            "AND completed_cases <= total_cases",
            name="ck_rag_evaluation_jobs_progress",
        ),
        sa.CheckConstraint(
            "length(model_catalog_hash) = 64",
            name="ck_rag_evaluation_jobs_model_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_job_id"],
            ["rag_evaluation_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["rag_evaluation_datasets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rag_evaluation_jobs_created_by_user_id",
        "rag_evaluation_jobs",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_rag_evaluation_jobs_dataset_created",
        "rag_evaluation_jobs",
        ["dataset_id", "created_at"],
    )
    op.create_index(
        "ix_rag_evaluation_jobs_status_created",
        "rag_evaluation_jobs",
        ["status", "created_at"],
    )

    op.create_table(
        "rag_evaluation_cases",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("generated_answer", sa.Text(), nullable=True),
        sa.Column("judge_reason", sa.Text(), nullable=True),
        sa.Column("observation_json", sa.JSON(), nullable=True),
        sa.Column("judge_json", sa.JSON(), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("checks_json", sa.JSON(), nullable=False),
        sa.Column("retrieved_identity_json", sa.JSON(), nullable=False),
        sa.Column("provider_error_code", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_rag_evaluation_cases_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["rag_evaluation_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "case_id",
            name="uq_rag_evaluation_case_job_case",
        ),
    )
    op.create_index(
        "ix_rag_evaluation_cases_job_position",
        "rag_evaluation_cases",
        ["job_id", "position"],
    )
    op.create_index(
        "ix_rag_evaluation_cases_job_status",
        "rag_evaluation_cases",
        ["job_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rag_evaluation_cases_job_status",
        table_name="rag_evaluation_cases",
    )
    op.drop_index(
        "ix_rag_evaluation_cases_job_position",
        table_name="rag_evaluation_cases",
    )
    op.drop_table("rag_evaluation_cases")
    op.drop_index(
        "ix_rag_evaluation_jobs_status_created",
        table_name="rag_evaluation_jobs",
    )
    op.drop_index(
        "ix_rag_evaluation_jobs_dataset_created",
        table_name="rag_evaluation_jobs",
    )
    op.drop_index(
        "ix_rag_evaluation_jobs_created_by_user_id",
        table_name="rag_evaluation_jobs",
    )
    op.drop_table("rag_evaluation_jobs")
    op.drop_index(
        "ix_rag_evaluation_datasets_name",
        table_name="rag_evaluation_datasets",
    )
    op.drop_index(
        "ix_rag_evaluation_datasets_created_by_user_id",
        table_name="rag_evaluation_datasets",
    )
    op.drop_index(
        "ix_rag_evaluation_datasets_created_at",
        table_name="rag_evaluation_datasets",
    )
    op.drop_table("rag_evaluation_datasets")
