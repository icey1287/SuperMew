import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from backend.infra.database import alembic_config


class DocumentCompatibilityRemovalMigrationTests(unittest.TestCase):
    def test_terminal_cleaned_rows_are_removed_before_schema_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "document-compatibility.db"
            url = f"sqlite:///{database_path}"
            config = alembic_config(url)
            command.upgrade(config, "0011_refresh_token_retention")
            engine = create_engine(url)
            now = datetime(2026, 7, 17, 12, 0, 0)
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
                        "(id, tenant_id, name, owner_id, status, catalog_revision, "
                        "created_at, updated_at) "
                        "VALUES ('kb-1', 'tenant-a', 'default', 1, 'active', 0, "
                        ":now, :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO documents "
                        "(id, tenant_id, knowledge_base_id, canonical_name, owner_id, "
                        "current_version_id, pending_version_id, publication_fence, "
                        "version_counter, status, deleted_at, created_at, updated_at) "
                        "VALUES ('doc-1', 'tenant-a', 'kb-1', 'guide.pdf', 1, NULL, "
                        "NULL, 1, 1, 'deleted', :now, :now, :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO document_versions "
                        "(id, document_id, content_sha256, build_fingerprint, "
                        "version_number, source_object_key, media_type, size_bytes, "
                        "parser_version, chunker_version, embedding_model, index_version, "
                        "storage_layout, vector_collection, legacy_identity, status, "
                        "chunk_count, parent_chunk_count, superseded_at, cleanup_after, "
                        "index_cleaned_at, created_at, updated_at) "
                        "VALUES ('version-old', 'doc-1', :content_hash, :build_hash, 1, "
                        "'guide.pdf', '', 0, 'retired', 'retired', 'retired', "
                        "'retired', 'legacy_filename', 'documents', 'source-old', "
                        "'superseded', 0, 0, :now, :now, :now, :now, :now)"
                    ),
                    {
                        "content_hash": "a" * 64,
                        "build_hash": "b" * 64,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO document_cleanup_jobs "
                        "(id, document_version_id, status, current_step, attempts, "
                        "max_attempts, execution_fence, step_state_json, finished_at, "
                        "created_at, updated_at) "
                        "VALUES ('cleanup-old', 'version-old', 'completed', 'finalize', "
                        "1, 3, 1, '{}', :now, :now, :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO document_retirement_jobs "
                        "(id, document_id, tenant_id, canonical_name, publication_fence, "
                        "cleanup_version_ids_json, created_at, updated_at) "
                        "VALUES ('retire-old', 'doc-1', 'tenant-a', 'guide.pdf', 1, "
                        "'[\"version-old\"]', :now, :now)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO parent_chunks "
                        "(chunk_id, tenant_id, knowledge_base_id, document_id, "
                        "document_version_id, section_id, index_version, acl_tags, "
                        "content_hash, text, filename, file_type, file_path, page_number, "
                        "parent_chunk_id, root_chunk_id, chunk_level, chunk_idx, updated_at) "
                        "VALUES ('parent-old', 'default', '', '', '', '', 'v1', '[]', '', "
                        "'body', 'guide.pdf', 'PDF', 'guide.pdf', 1, '', 'parent-old', "
                        "1, 0, :now)"
                    ),
                    {"now": now},
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(url)
            inspector = inspect(engine)
            self.assertNotIn("document_catalog_states", inspector.get_table_names())
            version_columns = {
                column["name"] for column in inspector.get_columns("document_versions")
            }
            self.assertNotIn("storage_layout", version_columns)
            self.assertNotIn("legacy_identity", version_columns)
            with engine.connect() as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM document_versions "
                            "WHERE id = 'version-old'"
                        )
                    ).scalar_one(),
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM parent_chunks "
                            "WHERE chunk_id = 'parent-old'"
                        )
                    ).scalar_one(),
                )
                cleanup_ids = connection.execute(
                    text(
                        "SELECT cleanup_version_ids_json "
                        "FROM document_retirement_jobs WHERE id = 'retire-old'"
                    )
                ).scalar_one()
            self.assertEqual([], json.loads(cleanup_ids))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
