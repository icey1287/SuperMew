import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from backend.infra.database import alembic_config


class DocumentCatalogMigrationTests(unittest.TestCase):
    def test_0006_to_0007_backfills_catalog_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "catalog.db"
            url = f"sqlite:///{database_path}"
            config = alembic_config(url)
            command.upgrade(config, "0006_native_checkpoints")
            engine = create_engine(url)
            now = datetime(2026, 7, 15, 12, 0, 0)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, username, password_hash, role, created_at) "
                        "VALUES (1, 'alice', 'hash', 'user', :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO knowledge_bases "
                        "(id, tenant_id, name, owner_id, status, created_at, updated_at) "
                        "VALUES ('kb-1', 'tenant-a', 'default', 1, 'active', :now, :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO documents "
                        "(id, tenant_id, knowledge_base_id, canonical_name, owner_id, "
                        "current_version_id, status, created_at, updated_at) "
                        "VALUES ('doc-1', 'tenant-a', 'kb-1', 'guide.pdf', 1, "
                        "'version-1', 'ready', :now, :now)"
                    ),
                    {"now": now},
                )
                for version_id, content_hash, created_at in (
                    ("version-1", "1" * 64, now),
                    ("version-2", "2" * 64, now.replace(second=1)),
                ):
                    connection.execute(
                        text(
                            "INSERT INTO document_versions "
                            "(id, document_id, content_sha256, source_object_key, "
                            "media_type, size_bytes, parser_version, chunker_version, "
                            "embedding_model, index_version, status, chunk_count, "
                            "error_code, error_detail_redacted, created_at, updated_at) "
                            "VALUES (:id, 'doc-1', :content_hash, :source, "
                            "'application/pdf', 10, 'v1', 'v1', 'embed-v1', 'v1', "
                            "'ready', 1, NULL, NULL, :created_at, :created_at)"
                        ),
                        {
                            "id": version_id,
                            "content_hash": content_hash,
                            "source": f"objects/{content_hash}",
                            "created_at": created_at,
                        },
                    )
                connection.execute(
                    text(
                        "INSERT INTO index_jobs "
                        "(id, document_version_id, status, current_step, progress, "
                        "attempts, max_attempts, owner_worker_id, lease_expires_at, "
                        "heartbeat_at, next_retry_at, error_code, error_detail_redacted, "
                        "step_state_json, created_at, updated_at) "
                        "VALUES ('job-1', 'version-2', 'pending', 'uploaded', 0, "
                        "0, 3, NULL, NULL, NULL, NULL, NULL, NULL, '{}', :now, :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO index_manifests "
                        "(document_version_id, chunk_id, section_id, content_hash, indexed_at) "
                        "VALUES ('version-1', 'chunk-1', 'section-1', :hash, :now)"
                    ),
                    {"hash": "3" * 64, "now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO parent_chunks "
                        "(chunk_id, tenant_id, knowledge_base_id, document_id, "
                        "document_version_id, section_id, index_version, acl_tags, text, "
                        "filename, file_type, file_path, page_number, parent_chunk_id, "
                        "root_chunk_id, chunk_level, chunk_idx, updated_at) "
                        "VALUES ('parent-1', 'tenant-a', 'kb-1', 'doc-1', 'version-1', "
                        "'section-1', 'v1', '[]', 'text', 'guide.pdf', 'pdf', "
                        "'guide.pdf', 1, '', 'parent-1', 0, 0, :now)"
                    ),
                    {"now": now},
                )
            engine.dispose()

            command.upgrade(config, "0007_document_publication")
            engine = create_engine(url)
            inspector = inspect(engine)
            self.assertTrue(
                {"pending_version_id", "publication_fence", "version_counter"}
                <= {column["name"] for column in inspector.get_columns("documents")}
            )
            self.assertTrue(
                {
                    "version_number",
                    "build_fingerprint",
                    "storage_layout",
                    "index_cleaned_at",
                    "cleanup_error_code",
                }
                <= {
                    column["name"]
                    for column in inspector.get_columns("document_versions")
                }
            )
            self.assertTrue(
                {
                    "legacy_collection",
                    "legacy_knowledge_base_id",
                    "legacy_knowledge_base_name",
                    "legacy_adoption_fence",
                    "legacy_adoption_completed_at",
                    "legacy_corpus_fingerprint",
                }
                <= {
                    column["name"]
                    for column in inspector.get_columns("document_catalog_states")
                }
            )
            self.assertIn(
                "content_hash",
                {column["name"] for column in inspector.get_columns("parent_chunks")},
            )
            self.assertEqual(
                {"fk_documents_current_version", "fk_documents_pending_version"},
                {
                    foreign_key["name"]
                    for foreign_key in inspector.get_foreign_keys("documents")
                    if foreign_key["name"]
                },
            )
            self.assertIn(
                "ck_document_versions_status",
                {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints(
                        "document_versions"
                    )
                },
            )
            self.assertIn(
                "ck_index_jobs_status",
                {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints("index_jobs")
                },
            )
            active_identity_indexes = {
                index["name"]: index
                for index in inspector.get_indexes("document_versions")
            }
            self.assertTrue(
                active_identity_indexes["uq_document_content_build_active"]["unique"]
            )
            with engine.connect() as connection:
                document = connection.execute(
                    text(
                        "SELECT current_version_id, version_counter "
                        "FROM documents WHERE id = 'doc-1'"
                    )
                ).one()
                versions = connection.execute(
                    text(
                        "SELECT id, version_number, build_fingerprint, storage_layout "
                        "FROM document_versions ORDER BY version_number"
                    )
                ).all()
                manifest = connection.execute(
                    text(
                        "SELECT store_kind, chunk_level FROM index_manifests "
                        "WHERE chunk_id = 'chunk-1'"
                    )
                ).one()
                parent_hash = connection.execute(
                    text(
                        "SELECT content_hash FROM parent_chunks "
                        "WHERE chunk_id = 'parent-1'"
                    )
                ).scalar_one()
                adoption_state_count = connection.execute(
                    text("SELECT COUNT(*) FROM document_catalog_states")
                ).scalar_one()
            self.assertEqual(("version-1", 2), tuple(document))
            self.assertEqual([1, 2], [row.version_number for row in versions])
            self.assertTrue(all(len(row.build_fingerprint) == 64 for row in versions))
            self.assertTrue(all(row.storage_layout == "versioned" for row in versions))
            self.assertEqual(("vector", 0), tuple(manifest))
            self.assertEqual("", parent_hash)
            self.assertEqual(0, adoption_state_count)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO document_versions "
                        "(id, document_id, content_sha256, build_fingerprint, "
                        "version_number, source_object_key, media_type, size_bytes, "
                        "parser_version, chunker_version, embedding_model, index_version, "
                        "storage_layout, vector_collection, status, chunk_count, "
                        "parent_chunk_count, created_at, updated_at) "
                        "SELECT 'version-3', document_id, content_sha256, "
                        "build_fingerprint, 3, 'objects/retry', media_type, size_bytes, "
                        "parser_version, chunker_version, embedding_model, index_version, "
                        "storage_layout, vector_collection, 'failed', 0, 0, "
                        ":now, :now FROM document_versions WHERE id = 'version-1'"
                    ),
                    {"now": now.replace(second=2)},
                )
                duplicate_count = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM document_versions "
                        "WHERE document_id = 'doc-1' AND content_sha256 = :digest"
                    ),
                    {"digest": "1" * 64},
                ).scalar_one()
            self.assertEqual(2, duplicate_count)
            engine.dispose()

            command.downgrade(config, "0006_native_checkpoints")
            engine = create_engine(url)
            inspector = inspect(engine)
            self.assertNotIn(
                "pending_version_id",
                {column["name"] for column in inspector.get_columns("documents")},
            )
            self.assertNotIn(
                "build_fingerprint",
                {
                    column["name"]
                    for column in inspector.get_columns("document_versions")
                },
            )
            self.assertNotIn("document_catalog_states", inspector.get_table_names())
            original_unique = {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("document_versions")
            }
            self.assertEqual(
                ("document_id", "content_sha256"),
                original_unique["uq_document_content_hash"],
            )
            with engine.connect() as connection:
                surviving_duplicate_identity = connection.execute(
                    text(
                        "SELECT id FROM document_versions "
                        "WHERE document_id = 'doc-1' AND content_sha256 = :digest"
                    ),
                    {"digest": "1" * 64},
                ).all()
            self.assertEqual([("version-1",)], surviving_duplicate_identity)
            engine.dispose()

            command.upgrade(config, "0007_document_publication")
            command.downgrade(config, "0006_native_checkpoints")


if __name__ == "__main__":
    unittest.main()
