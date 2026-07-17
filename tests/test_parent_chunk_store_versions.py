import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.models import ParentChunk


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeCache:
    def __init__(self):
        self.values = {}
        self.deleted = []
        self.fail_on_delete = None
        self.failed_once = False

    def get_json(self, key):
        return self.values.get(key)

    def set_json(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.deleted.append(key)
        if key == self.fail_on_delete and not self.failed_once:
            self.failed_once = True
            raise ConnectionError("cache unavailable")
        self.values.pop(key, None)

    def delete_strict(self, key):
        self.delete(key)
        return 1


def load_parent_store_module(session_factory, fake_cache):
    fake_indexing = types.ModuleType("backend.indexing")
    fake_indexing.__path__ = []

    fake_cache_module = types.ModuleType("backend.infra.cache")
    fake_cache_module.cache = fake_cache
    fake_database = types.ModuleType("backend.infra.database")
    fake_database.SessionLocal = session_factory

    with patch.dict(
        sys.modules,
        {
            "backend.indexing": fake_indexing,
            "backend.infra.cache": fake_cache_module,
            "backend.infra.database": fake_database,
        },
    ):
        path = REPO_ROOT / "backend" / "indexing" / "parent_chunk_store.py"
        spec = importlib.util.spec_from_file_location("parent_store_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def parent_chunk(chunk_id, *, version="version-1", tenant="tenant-1", index="v2"):
    return ParentChunk(
        chunk_id=chunk_id,
        tenant_id=tenant,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        document_version_id=version,
        section_id="section-1",
        index_version=index,
        acl_tags=["team:legal"],
        content_hash="a" * 64,
        text=f"text {chunk_id}",
        filename="doc.pdf",
        file_type="PDF",
        file_path="/tmp/doc.pdf",
        page_number=1,
        parent_chunk_id="",
        root_chunk_id=chunk_id,
        chunk_level=1,
        chunk_idx=0,
    )


class ParentChunkStoreVersionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        ParentChunk.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.cache = FakeCache()
        self.module = load_parent_store_module(self.session_factory, self.cache)
        self.store = self.module.ParentChunkStore()

    def tearDown(self):
        self.engine.dispose()

    def test_upsert_persists_and_caches_complete_artifact_metadata(self):
        document = {
            "chunk_id": "version-1::doc.pdf::p1::l1::0",
            "text": "parent text",
            "filename": "doc.pdf",
            "file_type": "PDF",
            "file_path": "/tmp/doc.pdf",
            "page_number": 1,
            "parent_chunk_id": "",
            "root_chunk_id": "version-1::doc.pdf::p1::l1::0",
            "chunk_level": 1,
            "chunk_idx": 0,
            "tenant_id": "tenant-1",
            "knowledge_base_id": "kb-1",
            "document_id": "doc-1",
            "document_version_id": "version-1",
            "section_id": "section-1",
            "index_version": "index-v2",
            "acl_tags": ["team:legal"],
            "content_hash": "b" * 64,
        }

        self.assertEqual(1, self.store.upsert_documents([document]))

        with self.session_factory() as db:
            row = db.get(ParentChunk, document["chunk_id"])
            self.assertEqual("tenant-1", row.tenant_id)
            self.assertEqual("kb-1", row.knowledge_base_id)
            self.assertEqual("doc-1", row.document_id)
            self.assertEqual("version-1", row.document_version_id)
            self.assertEqual("section-1", row.section_id)
            self.assertEqual("index-v2", row.index_version)
            self.assertEqual(["team:legal"], row.acl_tags)
            self.assertEqual("b" * 64, row.content_hash)
        cached = self.cache.values[f"parent_chunk:{document['chunk_id']}"]
        self.assertEqual("version-1", cached["document_version_id"])
        self.assertEqual("b" * 64, cached["content_hash"])

    def test_get_by_ids_discards_cache_payload_without_document_version_identity(self):
        with self.session_factory() as db:
            db.add(parent_chunk("db-chunk"))
            db.commit()
        stale_payload = {"chunk_id": "stale", "text": "stale cached text"}
        self.cache.values["parent_chunk:stale"] = stale_payload

        results = self.store.get_documents_by_ids(["stale", "db-chunk"])

        self.assertEqual(["db-chunk"], [item["chunk_id"] for item in results])
        self.assertEqual("version-1", results[0]["document_version_id"])
        self.assertEqual("a" * 64, results[0]["content_hash"])
        self.assertEqual(["parent_chunk:stale"], self.cache.deleted)

    def test_count_and_delete_are_isolated_to_exact_version_scope(self):
        rows = [
            parent_chunk("target-1"),
            parent_chunk("target-2"),
            parent_chunk("other-version", version="version-2"),
            parent_chunk("other-tenant", tenant="tenant-2"),
            parent_chunk("other-index", index="v3"),
        ]
        with self.session_factory() as db:
            db.add_all(rows)
            db.commit()
        for row in rows:
            self.cache.values[f"parent_chunk:{row.chunk_id}"] = {
                "chunk_id": row.chunk_id
            }

        count = self.store.count_by_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="v2",
        )
        deleted = self.store.delete_by_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="v2",
        )

        self.assertEqual(2, count)
        self.assertEqual(2, deleted)
        self.assertCountEqual(
            ["parent_chunk:target-1", "parent_chunk:target-2"],
            self.cache.deleted,
        )
        with self.session_factory() as db:
            self.assertIsNone(db.get(ParentChunk, "target-1"))
            self.assertIsNone(db.get(ParentChunk, "target-2"))
            self.assertIsNotNone(db.get(ParentChunk, "other-version"))
            self.assertIsNotNone(db.get(ParentChunk, "other-tenant"))
            self.assertIsNotNone(db.get(ParentChunk, "other-index"))

    def test_verify_version_checks_ids_count_and_artifact_metadata(self):
        with self.session_factory() as db:
            db.add_all([parent_chunk("chunk-1"), parent_chunk("chunk-2")])
            db.commit()

        exact = self.store.verify_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="v2",
            expected_chunk_ids=["chunk-2", "chunk-1"],
        )

        self.assertTrue(exact.exact)
        self.assertEqual(2, exact.expected_count)
        self.assertEqual(2, exact.actual_count)
        self.assertEqual((), exact.metadata_mismatch_ids)

        with self.session_factory() as db:
            row = db.get(ParentChunk, "chunk-2")
            row.content_hash = ""
            db.commit()
        mismatch = self.store.verify_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="v2",
            expected_chunk_ids=["chunk-1", "missing"],
        )

        self.assertFalse(mismatch.exact)
        self.assertEqual(("missing",), mismatch.missing_ids)
        self.assertEqual(("chunk-2",), mismatch.unexpected_ids)
        self.assertEqual(("chunk-2",), mismatch.metadata_mismatch_ids)

    def test_cache_delete_failure_keeps_db_rows_for_idempotent_retry(self):
        rows = [parent_chunk("target-1"), parent_chunk("target-2")]
        with self.session_factory() as db:
            db.add_all(rows)
            db.commit()
        for row in rows:
            self.cache.values[f"parent_chunk:{row.chunk_id}"] = {
                "chunk_id": row.chunk_id
            }
        self.cache.fail_on_delete = "parent_chunk:target-2"

        with self.assertRaises(ConnectionError):
            self.store.delete_by_version(
                tenant_id="tenant-1",
                knowledge_base_id="kb-1",
                document_id="doc-1",
                document_version_id="version-1",
                index_version="v2",
            )

        with self.session_factory() as db:
            self.assertIsNotNone(db.get(ParentChunk, "target-1"))
            self.assertIsNotNone(db.get(ParentChunk, "target-2"))

        deleted = self.store.delete_by_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="v2",
        )

        self.assertEqual(2, deleted)
        with self.session_factory() as db:
            self.assertIsNone(db.get(ParentChunk, "target-1"))
            self.assertIsNone(db.get(ParentChunk, "target-2"))

    def test_version_scope_requires_non_empty_identity(self):
        with self.assertRaises(ValueError):
            self.store.count_by_version(
                tenant_id="",
                knowledge_base_id="kb-1",
                document_id="doc-1",
                document_version_id="version-1",
            )


if __name__ == "__main__":
    unittest.main()
