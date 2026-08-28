"""remove retired document compatibility schema

Revision ID: 0012_remove_document_compat
Revises: 0011_refresh_token_retention
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op


revision = "0012_remove_document_compat"
down_revision = "0011_refresh_token_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    retired_versions = (
        connection.execute(
            sa.text(
                "SELECT dv.id, dv.document_id, dv.status, dv.index_cleaned_at, "
                "d.current_version_id, d.pending_version_id "
                "FROM document_versions dv "
                "JOIN documents d ON d.id = dv.document_id "
                "WHERE dv.storage_layout <> 'versioned' "
                "OR (dv.legacy_identity IS NOT NULL AND dv.legacy_identity <> '')"
            )
        )
        .mappings()
        .all()
    )
    unsafe_versions = [
        row
        for row in retired_versions
        if row["current_version_id"] == row["id"]
        or row["pending_version_id"] == row["id"]
        or row["status"] not in {"failed", "superseded"}
        or row["index_cleaned_at"] is None
    ]
    if unsafe_versions:
        raise RuntimeError(
            "active or uncleaned document compatibility data still exists"
        )

    retired_version_ids = {row["id"] for row in retired_versions}
    if retired_version_ids:
        retirement_jobs = sa.table(
            "document_retirement_jobs",
            sa.column("id", sa.String()),
            sa.column("cleanup_version_ids_json", sa.JSON()),
        )
        for row in connection.execute(
            sa.select(
                retirement_jobs.c.id,
                retirement_jobs.c.cleanup_version_ids_json,
            )
        ).mappings():
            cleanup_ids = list(row["cleanup_version_ids_json"] or [])
            current_ids = [
                version_id
                for version_id in cleanup_ids
                if version_id not in retired_version_ids
            ]
            if current_ids != cleanup_ids:
                connection.execute(
                    retirement_jobs.update()
                    .where(retirement_jobs.c.id == row["id"])
                    .values(cleanup_version_ids_json=current_ids)
                )

        version_filter = sa.bindparam("retired_version_ids", expanding=True)
        for table_name in (
            "index_manifests",
            "document_cleanup_jobs",
            "index_jobs",
        ):
            connection.execute(
                sa.text(
                    f"DELETE FROM {table_name} "
                    "WHERE document_version_id IN :retired_version_ids"
                ).bindparams(version_filter),
                {"retired_version_ids": sorted(retired_version_ids)},
            )
        connection.execute(
            sa.text(
                "DELETE FROM document_versions WHERE id IN :retired_version_ids"
            ).bindparams(version_filter),
            {"retired_version_ids": sorted(retired_version_ids)},
        )

    connection.execute(
        sa.text(
            "DELETE FROM parent_chunks WHERE "
            "COALESCE(knowledge_base_id, '') = '' "
            "OR COALESCE(document_id, '') = '' "
            "OR COALESCE(document_version_id, '') = '' "
            "OR COALESCE(section_id, '') = '' "
            "OR COALESCE(index_version, '') = '' "
            "OR COALESCE(content_hash, '') = ''"
        )
    )

    with op.batch_alter_table("document_versions") as batch:
        batch.drop_constraint("uq_legacy_source_identity", type_="unique")
        batch.drop_column("legacy_identity")
        batch.drop_column("storage_layout")
    op.drop_table("document_catalog_states")


def downgrade() -> None:
    raise RuntimeError("document compatibility schema removal is irreversible")
