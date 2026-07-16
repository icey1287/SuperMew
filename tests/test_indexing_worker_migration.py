import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy import create_engine, inspect, text

from backend.db.models import (
    DocumentCleanupJob,
    DocumentRetirementJob,
    IndexJob,
    WorkerHeartbeat,
)
from backend.infra.database import alembic_config


class IndexingWorkerMigrationTests(unittest.TestCase):
    @staticmethod
    def _seed_0007(url: str) -> None:
        engine = create_engine(url)
        now = datetime(2026, 7, 15, 12, 0, 0)
        cleanup_after = now + timedelta(minutes=5)
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
                    "current_version_id, pending_version_id, publication_fence, "
                    "version_counter, status, created_at, updated_at) "
                    "VALUES ('doc-1', 'tenant-a', 'kb-1', 'guide.pdf', 1, "
                    "NULL, NULL, 0, 5, 'failed', :now, :now)"
                ),
                {"now": now},
            )
            versions = (
                ("version-failed", "failed", cleanup_after, None),
                ("version-superseded", "superseded", cleanup_after, None),
                ("version-cleaned", "failed", cleanup_after, now),
                ("version-ready", "ready", cleanup_after, None),
                ("version-unscheduled", "failed", None, None),
            )
            for number, (version_id, status, scheduled_at, cleaned_at) in enumerate(
                versions, start=1
            ):
                connection.execute(
                    text(
                        "INSERT INTO document_versions "
                        "(id, document_id, content_sha256, build_fingerprint, "
                        "version_number, source_object_key, media_type, size_bytes, "
                        "parser_version, chunker_version, embedding_model, index_version, "
                        "storage_layout, vector_collection, status, chunk_count, "
                        "parent_chunk_count, cleanup_after, index_cleaned_at, "
                        "created_at, updated_at) "
                        "VALUES (:id, 'doc-1', :content_hash, :build_fingerprint, "
                        ":version_number, :source, 'application/pdf', 10, 'v1', 'v1', "
                        "'embed-v1', 'v1', 'versioned', 'documents', :status, 0, 0, "
                        ":cleanup_after, :index_cleaned_at, :now, :now)"
                    ),
                    {
                        "id": version_id,
                        "content_hash": f"{number:x}" * 64,
                        "build_fingerprint": f"{number + 5:x}" * 64,
                        "version_number": number,
                        "source": f"objects/{version_id}",
                        "status": status,
                        "cleanup_after": scheduled_at,
                        "index_cleaned_at": cleaned_at,
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO index_jobs "
                    "(id, document_version_id, status, current_step, progress, attempts, "
                    "max_attempts, publication_fence, expected_current_version_id, "
                    "owner_worker_id, lease_expires_at, heartbeat_at, next_retry_at, "
                    "error_code, error_detail_redacted, step_state_json, finished_at, "
                    "created_at, updated_at) "
                    "VALUES ('job-1', 'version-failed', 'failed', 'failed', 0, 1, 3, "
                    "1, NULL, NULL, NULL, NULL, NULL, 'INDEX_FAILED', NULL, '{}', :now, "
                    ":now, :now)"
                ),
                {"now": now},
            )
        engine.dispose()

    def test_0007_to_0008_adds_worker_schema_and_backfills_cleanup_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "worker.db"
            url = f"sqlite:///{database_path}"
            config = alembic_config(url)
            command.upgrade(config, "0007_document_publication")
            self._seed_0007(url)

            command.upgrade(config, "0008_indexing_worker")
            engine = create_engine(url)
            inspector = inspect(engine)
            self.assertTrue(
                {
                    "document_cleanup_jobs",
                    "document_retirement_jobs",
                    "worker_heartbeats",
                }
                <= set(inspector.get_table_names())
            )
            index_job_columns = {
                column["name"]: column for column in inspector.get_columns("index_jobs")
            }
            self.assertIn("execution_fence", index_job_columns)
            self.assertIn("started_at", index_job_columns)
            self.assertIsInstance(
                index_job_columns["execution_fence"]["type"], sa.BigInteger
            )
            self.assertEqual(
                {
                    "ix_index_jobs_claim_ready",
                    "ix_index_jobs_claim_expired",
                },
                {
                    index["name"]
                    for index in inspector.get_indexes("index_jobs")
                    if index["name"].startswith("ix_index_jobs_claim_")
                },
            )
            cleanup_columns = {
                column["name"]: column
                for column in inspector.get_columns("document_cleanup_jobs")
            }
            self.assertTrue(
                {
                    "status",
                    "current_step",
                    "attempts",
                    "max_attempts",
                    "owner_worker_id",
                    "execution_fence",
                    "lease_expires_at",
                    "heartbeat_at",
                    "next_retry_at",
                    "error_code",
                    "error_detail_redacted",
                    "step_state_json",
                    "started_at",
                    "finished_at",
                }
                <= set(cleanup_columns)
            )
            self.assertIsInstance(
                cleanup_columns["execution_fence"]["type"], sa.BigInteger
            )
            self.assertIn(
                "ck_document_cleanup_jobs_status",
                {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints(
                        "document_cleanup_jobs"
                    )
                },
            )
            self.assertIn(
                "uq_document_cleanup_job_document_version",
                {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints(
                        "document_cleanup_jobs"
                    )
                },
            )
            self.assertEqual(
                ["worker_kind", "status", "heartbeat_at"],
                {
                    index["name"]: index["column_names"]
                    for index in inspector.get_indexes("worker_heartbeats")
                }["ix_worker_heartbeats_readiness"],
            )
            self.assertIn(
                "ck_worker_heartbeats_status",
                {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints(
                        "worker_heartbeats"
                    )
                },
            )
            retirement_columns = {
                column["name"]
                for column in inspector.get_columns("document_retirement_jobs")
            }
            self.assertTrue(
                {
                    "id",
                    "document_id",
                    "tenant_id",
                    "canonical_name",
                    "publication_fence",
                    "cleanup_version_ids_json",
                    "error_code",
                }
                <= retirement_columns
            )
            with engine.connect() as connection:
                jobs = connection.execute(
                    text(
                        "SELECT document_version_id, status, current_step, attempts, "
                        "max_attempts, execution_fence, "
                        "julianday(next_retry_at) = julianday(("
                        "SELECT cleanup_after FROM document_versions "
                        "WHERE document_versions.id = "
                        "document_cleanup_jobs.document_version_id"
                        ")) AS uses_cleanup_schedule "
                        "FROM document_cleanup_jobs ORDER BY document_version_id"
                    )
                ).all()
                existing_job = connection.execute(
                    text(
                        "SELECT execution_fence, started_at FROM index_jobs "
                        "WHERE id = 'job-1'"
                    )
                ).one()
            self.assertEqual(
                ["version-failed", "version-superseded"],
                [row.document_version_id for row in jobs],
            )
            self.assertTrue(
                all(
                    tuple(row)[1:] == ("pending", "pending", 0, 3, 0, 1) for row in jobs
                )
            )
            self.assertEqual((0, None), tuple(existing_job))
            engine.dispose()

            command.downgrade(config, "0007_document_publication")
            engine = create_engine(url)
            inspector = inspect(engine)
            self.assertNotIn("document_cleanup_jobs", inspector.get_table_names())
            self.assertNotIn("worker_heartbeats", inspector.get_table_names())
            self.assertNotIn(
                "document_retirement_jobs",
                inspector.get_table_names(),
            )
            self.assertFalse(
                {"execution_fence", "started_at"}
                & {column["name"] for column in inspector.get_columns("index_jobs")}
            )
            engine.dispose()

            command.upgrade(config, "0008_indexing_worker")
            engine = create_engine(url)
            with engine.connect() as connection:
                self.assertEqual(
                    2,
                    connection.execute(
                        text("SELECT COUNT(*) FROM document_cleanup_jobs")
                    ).scalar_one(),
                )
            engine.dispose()

    def test_orm_metadata_matches_worker_schema_contract(self):
        self.assertIsInstance(IndexJob.__table__.c.execution_fence.type, sa.BigInteger)
        self.assertIn("started_at", IndexJob.__table__.c)
        self.assertIsInstance(
            DocumentCleanupJob.__table__.c.execution_fence.type, sa.BigInteger
        )
        self.assertEqual(
            {"worker_id", "worker_kind", "status", "heartbeat_at", "metadata_json"},
            {
                name
                for name in WorkerHeartbeat.__table__.c.keys()
                if name
                in {
                    "worker_id",
                    "worker_kind",
                    "status",
                    "heartbeat_at",
                    "metadata_json",
                }
            },
        )
        self.assertIsInstance(
            DocumentRetirementJob.__table__.c.publication_fence.type,
            sa.BigInteger,
        )


if __name__ == "__main__":
    unittest.main()
