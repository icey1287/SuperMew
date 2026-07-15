"""document catalog publication fencing and version-owned storage metadata

Revision ID: 0007_document_publication
Revises: 0006_native_checkpoints
Create Date: 2026-07-15
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "0007_document_publication"
down_revision = "0006_native_checkpoints"
branch_labels = None
depends_on = None


def _build_fingerprint(row: sa.Row) -> str:
    payload = json.dumps(
        {
            "schema_version": 1,
            "parser_version": row.parser_version or "v1",
            "chunker_version": row.chunker_version or "v1",
            "embedding_model": row.embedding_model or "",
            "index_version": row.index_version or "v1",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collapse_duplicate_content_versions_for_downgrade(connection) -> None:
    """Fold PR-15 build/history variants into the single PR-03 identity."""

    versions = sa.table(
        "document_versions",
        sa.column("id", sa.String()),
        sa.column("document_id", sa.String()),
        sa.column("content_sha256", sa.String()),
        sa.column("version_number", sa.Integer()),
    )
    documents = sa.table(
        "documents",
        sa.column("id", sa.String()),
        sa.column("current_version_id", sa.String()),
        sa.column("pending_version_id", sa.String()),
    )
    rows = connection.execute(
        sa.select(
            versions.c.id,
            versions.c.document_id,
            versions.c.content_sha256,
            versions.c.version_number,
            documents.c.current_version_id,
            documents.c.pending_version_id,
        ).join(documents, documents.c.id == versions.c.document_id)
    ).all()
    grouped: dict[tuple[str, str], list[sa.Row]] = {}
    for row in rows:
        grouped.setdefault((row.document_id, row.content_sha256), []).append(row)

    discarded_ids: list[str] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        current_id = group[0].current_version_id
        pending_id = group[0].pending_version_id
        keeper = next((row for row in group if row.id == current_id), None)
        if keeper is None:
            keeper = next((row for row in group if row.id == pending_id), None)
        if keeper is None:
            keeper = max(group, key=lambda row: (row.version_number, row.id))
        discarded_ids.extend(row.id for row in group if row.id != keeper.id)

    if not discarded_ids:
        return
    connection.execute(
        documents.update()
        .where(documents.c.pending_version_id.in_(discarded_ids))
        .values(pending_version_id=None)
    )
    for table_name in ("index_manifests", "index_jobs", "parent_chunks"):
        table = sa.table(
            table_name,
            sa.column("document_version_id", sa.String()),
        )
        connection.execute(
            table.delete().where(table.c.document_version_id.in_(discarded_ids))
        )
    connection.execute(versions.delete().where(versions.c.id.in_(discarded_ids)))


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column("catalog_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "document_catalog_states",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("legacy_collection", sa.String(length=160), nullable=False),
        sa.Column("legacy_knowledge_base_id", sa.String(length=64), nullable=True),
        sa.Column("legacy_knowledge_base_name", sa.String(length=160), nullable=False),
        sa.Column(
            "legacy_adoption_fence",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("legacy_adoption_completed_at", sa.DateTime(), nullable=True),
        sa.Column("legacy_corpus_fingerprint", sa.CHAR(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.ForeignKeyConstraint(
            ["legacy_knowledge_base_id"],
            ["knowledge_bases.id"],
            ondelete="RESTRICT",
        ),
    )

    with op.batch_alter_table("document_versions") as batch:
        batch.drop_constraint("uq_document_content_hash", type_="unique")
        batch.add_column(
            sa.Column("build_fingerprint", sa.CHAR(length=64), nullable=True)
        )
        batch.add_column(sa.Column("version_number", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "storage_layout",
                sa.String(length=32),
                server_default="versioned",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "vector_collection",
                sa.String(length=160),
                server_default="",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("legacy_identity", sa.String(length=512), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "parent_chunk_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("cleanup_after", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("index_cleaned_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column("cleanup_error_code", sa.String(length=64), nullable=True)
        )

    connection = op.get_bind()
    versions = sa.table(
        "document_versions",
        sa.column("id", sa.String()),
        sa.column("document_id", sa.String()),
        sa.column("created_at", sa.DateTime()),
        sa.column("parser_version", sa.String()),
        sa.column("chunker_version", sa.String()),
        sa.column("embedding_model", sa.String()),
        sa.column("index_version", sa.String()),
        sa.column("build_fingerprint", sa.CHAR(length=64)),
        sa.column("version_number", sa.Integer()),
    )
    rows = connection.execute(
        sa.select(
            versions.c.id,
            versions.c.document_id,
            versions.c.created_at,
            versions.c.parser_version,
            versions.c.chunker_version,
            versions.c.embedding_model,
            versions.c.index_version,
        ).order_by(
            versions.c.document_id.asc(),
            versions.c.created_at.asc(),
            versions.c.id.asc(),
        )
    ).all()
    version_counters: dict[str, int] = {}
    for row in rows:
        version_number = version_counters.get(row.document_id, 0) + 1
        version_counters[row.document_id] = version_number
        connection.execute(
            versions.update()
            .where(versions.c.id == row.id)
            .values(
                build_fingerprint=_build_fingerprint(row),
                version_number=version_number,
            )
        )

    with op.batch_alter_table("document_versions") as batch:
        batch.alter_column(
            "build_fingerprint",
            existing_type=sa.CHAR(length=64),
            nullable=False,
        )
        batch.alter_column("version_number", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_document_version_number", ["document_id", "version_number"]
        )
        batch.create_unique_constraint(
            "uq_legacy_source_identity",
            ["vector_collection", "legacy_identity"],
        )
        batch.create_check_constraint(
            "ck_document_versions_status",
            "status IN ('uploaded', 'parsing', 'indexing', 'staged', "
            "'ready', 'failed', 'superseded')",
        )
    active_version_predicate = sa.text(
        "status IN ('uploaded', 'parsing', 'indexing', 'staged', 'ready')"
    )
    op.create_index(
        "uq_document_content_build_active",
        "document_versions",
        ["document_id", "content_sha256", "build_fingerprint"],
        unique=True,
        postgresql_where=active_version_predicate,
        sqlite_where=active_version_predicate,
    )
    op.create_index(
        "ix_document_versions_cleanup_after",
        "document_versions",
        ["cleanup_after"],
    )

    # Existing PR-03 rows never used the pointer in production. Preserve valid
    # values while clearing any legacy orphan before adding the real FK.
    op.execute(
        """
        UPDATE documents
        SET current_version_id = NULL
        WHERE current_version_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM document_versions
              WHERE document_versions.id = documents.current_version_id
                AND document_versions.document_id = documents.id
          )
        """
    )
    with op.batch_alter_table("documents") as batch:
        batch.add_column(
            sa.Column("pending_version_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "publication_fence",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "version_counter",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_documents_current_version",
            "document_versions",
            ["current_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_documents_pending_version",
            "document_versions",
            ["pending_version_id"],
            ["id"],
            ondelete="SET NULL",
        )

    documents = sa.table(
        "documents",
        sa.column("id", sa.String()),
        sa.column("version_counter", sa.Integer()),
    )
    for document_id, version_counter in version_counters.items():
        connection.execute(
            documents.update()
            .where(documents.c.id == document_id)
            .values(version_counter=version_counter)
        )

    with op.batch_alter_table("index_jobs") as batch:
        batch.add_column(
            sa.Column(
                "publication_fence",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "expected_current_version_id", sa.String(length=64), nullable=True
            )
        )
        batch.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))
        batch.create_unique_constraint(
            "uq_index_job_document_version", ["document_version_id"]
        )
        batch.create_check_constraint(
            "ck_index_jobs_status",
            "status IN ('pending', 'running', 'retry_wait', 'staged', "
            "'completed', 'failed', 'cancelled', 'dead_letter')",
        )

    with op.batch_alter_table("index_manifests") as batch:
        batch.drop_constraint("uq_index_manifest_chunk", type_="unique")
        batch.add_column(
            sa.Column(
                "store_kind",
                sa.String(length=32),
                server_default="vector",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("chunk_level", sa.Integer(), server_default="0", nullable=False)
        )
        batch.create_unique_constraint(
            "uq_index_manifest_chunk",
            ["document_version_id", "store_kind", "chunk_id"],
        )

    op.add_column(
        "parent_chunks",
        sa.Column(
            "content_hash",
            sa.CHAR(length=64),
            server_default="",
            nullable=False,
        ),
    )


def downgrade() -> None:
    _collapse_duplicate_content_versions_for_downgrade(op.get_bind())

    op.drop_column("parent_chunks", "content_hash")

    with op.batch_alter_table("index_manifests") as batch:
        batch.drop_constraint("uq_index_manifest_chunk", type_="unique")
        batch.create_unique_constraint(
            "uq_index_manifest_chunk", ["document_version_id", "chunk_id"]
        )
        batch.drop_column("chunk_level")
        batch.drop_column("store_kind")

    with op.batch_alter_table("index_jobs") as batch:
        batch.drop_constraint("ck_index_jobs_status", type_="check")
        batch.drop_constraint("uq_index_job_document_version", type_="unique")
        batch.drop_column("finished_at")
        batch.drop_column("expected_current_version_id")
        batch.drop_column("publication_fence")

    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("fk_documents_pending_version", type_="foreignkey")
        batch.drop_constraint("fk_documents_current_version", type_="foreignkey")
        batch.drop_column("deleted_at")
        batch.drop_column("publication_fence")
        batch.drop_column("version_counter")
        batch.drop_column("pending_version_id")

    op.drop_index("uq_document_content_build_active", table_name="document_versions")
    op.drop_index("ix_document_versions_cleanup_after", table_name="document_versions")
    with op.batch_alter_table("document_versions") as batch:
        batch.drop_constraint("ck_document_versions_status", type_="check")
        batch.drop_constraint("uq_legacy_source_identity", type_="unique")
        batch.drop_constraint("uq_document_version_number", type_="unique")
        batch.create_unique_constraint(
            "uq_document_content_hash", ["document_id", "content_sha256"]
        )
        batch.drop_column("cleanup_after")
        batch.drop_column("cleanup_error_code")
        batch.drop_column("index_cleaned_at")
        batch.drop_column("superseded_at")
        batch.drop_column("published_at")
        batch.drop_column("parent_chunk_count")
        batch.drop_column("legacy_identity")
        batch.drop_column("vector_collection")
        batch.drop_column("storage_layout")
        batch.drop_column("version_number")
        batch.drop_column("build_fingerprint")

    op.drop_table("document_catalog_states")
    op.drop_column("knowledge_bases", "catalog_revision")
