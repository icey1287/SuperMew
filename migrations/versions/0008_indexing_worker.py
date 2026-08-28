"""durable indexing worker leases, cleanup queue, and heartbeats

Revision ID: 0008_indexing_worker
Revises: 0007_document_publication
Create Date: 2026-07-15
"""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "0008_indexing_worker"
down_revision = "0007_document_publication"
branch_labels = None
depends_on = None


def _cleanup_job_id(document_version_id: str) -> str:
    digest = hashlib.sha256(document_version_id.encode("utf-8")).hexdigest()
    return f"cleanup_{digest[:48]}"


def _backfill_cleanup_jobs(connection) -> None:
    versions = sa.table(
        "document_versions",
        sa.column("id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("cleanup_after", sa.DateTime()),
        sa.column("index_cleaned_at", sa.DateTime()),
    )
    cleanup_jobs = sa.table(
        "document_cleanup_jobs",
        sa.column("id", sa.String()),
        sa.column("document_version_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("current_step", sa.String()),
        sa.column("attempts", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
        sa.column("owner_worker_id", sa.String()),
        sa.column("execution_fence", sa.BigInteger()),
        sa.column("lease_expires_at", sa.DateTime()),
        sa.column("heartbeat_at", sa.DateTime()),
        sa.column("next_retry_at", sa.DateTime()),
        sa.column("error_code", sa.String()),
        sa.column("error_detail_redacted", sa.Text()),
        sa.column("step_state_json", sa.JSON()),
        sa.column("started_at", sa.DateTime()),
        sa.column("finished_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    candidates = connection.execute(
        sa.select(versions.c.id, versions.c.cleanup_after).where(
            versions.c.status.in_(("failed", "superseded")),
            versions.c.cleanup_after.is_not(None),
            versions.c.index_cleaned_at.is_(None),
        )
    ).all()
    existing_version_ids = set(
        connection.execute(sa.select(cleanup_jobs.c.document_version_id)).scalars()
    )
    for candidate in candidates:
        if candidate.id in existing_version_ids:
            continue
        connection.execute(
            cleanup_jobs.insert().values(
                id=_cleanup_job_id(candidate.id),
                document_version_id=candidate.id,
                status="pending",
                current_step="pending",
                attempts=0,
                max_attempts=3,
                owner_worker_id=None,
                execution_fence=0,
                lease_expires_at=None,
                heartbeat_at=None,
                next_retry_at=candidate.cleanup_after,
                error_code=None,
                error_detail_redacted=None,
                step_state_json={},
                started_at=None,
                finished_at=None,
                created_at=sa.func.current_timestamp(),
                updated_at=sa.func.current_timestamp(),
            )
        )
        existing_version_ids.add(candidate.id)


def upgrade() -> None:
    with op.batch_alter_table("index_jobs") as batch:
        batch.add_column(
            sa.Column(
                "execution_fence",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))

    op.create_index(
        "ix_index_jobs_claim_ready",
        "index_jobs",
        ["status", "next_retry_at", "created_at"],
    )
    op.create_index(
        "ix_index_jobs_claim_expired",
        "index_jobs",
        ["status", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "document_cleanup_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_version_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "current_step",
            sa.String(length=64),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("owner_worker_id", sa.String(length=128), nullable=True),
        sa.Column(
            "execution_fence",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column(
            "step_state_json",
            sa.JSON(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', "
            "'completed', 'dead_letter')",
            name="ck_document_cleanup_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            name="uq_document_cleanup_job_document_version",
        ),
    )
    op.create_index(
        "ix_document_cleanup_jobs_claim_ready",
        "document_cleanup_jobs",
        ["status", "next_retry_at", "created_at"],
    )
    op.create_index(
        "ix_document_cleanup_jobs_claim_expired",
        "document_cleanup_jobs",
        ["status", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("worker_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="starting",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column(
            "metadata_json",
            sa.JSON(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('starting', 'running', 'draining', 'stopped')",
            name="ck_worker_heartbeats_status",
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_worker_heartbeats_readiness",
        "worker_heartbeats",
        ["worker_kind", "status", "heartbeat_at"],
    )

    op.create_table(
        "document_retirement_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("publication_fence", sa.BigInteger(), nullable=False),
        sa.Column(
            "cleanup_version_ids_json",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_retirement_jobs_document_id",
        "document_retirement_jobs",
        ["document_id"],
    )
    op.create_index(
        "ix_document_retirement_jobs_tenant_created",
        "document_retirement_jobs",
        ["tenant_id", "created_at"],
    )

    _backfill_cleanup_jobs(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_document_retirement_jobs_tenant_created",
        table_name="document_retirement_jobs",
    )
    op.drop_index(
        "ix_document_retirement_jobs_document_id",
        table_name="document_retirement_jobs",
    )
    op.drop_table("document_retirement_jobs")

    op.drop_index("ix_worker_heartbeats_readiness", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")

    op.drop_index(
        "ix_document_cleanup_jobs_claim_expired",
        table_name="document_cleanup_jobs",
    )
    op.drop_index(
        "ix_document_cleanup_jobs_claim_ready",
        table_name="document_cleanup_jobs",
    )
    op.drop_table("document_cleanup_jobs")

    op.drop_index("ix_index_jobs_claim_expired", table_name="index_jobs")
    op.drop_index("ix_index_jobs_claim_ready", table_name="index_jobs")
    with op.batch_alter_table("index_jobs") as batch:
        batch.drop_column("started_at")
        batch.drop_column("execution_fence")
