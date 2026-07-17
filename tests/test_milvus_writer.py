import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_milvus_writer_module():
    fake_indexing = types.ModuleType("backend.indexing")
    fake_indexing.__path__ = []

    fake_embedding = types.ModuleType("backend.indexing.embedding")

    class EmbeddingService:
        pass

    fake_embedding.EmbeddingService = EmbeddingService
    fake_embedding.embedding_service = None

    fake_client = types.ModuleType("backend.indexing.milvus_client")

    class MilvusStore:
        pass

    fake_client.MilvusStore = MilvusStore
    fake_client.get_milvus_store = lambda: None

    with patch.dict(
        sys.modules,
        {
            "backend.indexing": fake_indexing,
            "backend.indexing.embedding": fake_embedding,
            "backend.indexing.milvus_client": fake_client,
        },
    ):
        path = REPO_ROOT / "backend" / "indexing" / "milvus_writer.py"
        spec = importlib.util.spec_from_file_location("milvus_writer_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class FakeEmbeddingService:
    def __init__(self, events):
        self.events = events

    def get_embeddings(self, texts):
        self.events.append(("embed", list(texts)))
        return [[float(idx)] for idx, _ in enumerate(texts, start=1)]


class ShortEmbeddingService:
    def get_embeddings(self, texts):
        return [[1.0]] * max(len(texts) - 1, 0)


class VersionedStore:
    def __init__(self, events, collection_name="documents", registry=None):
        self.events = events
        self.collection_name = collection_name
        self.registry = registry if registry is not None else {}
        self.registry[collection_name] = self
        self.insert_responses = []
        self.verify_result = object()
        self.records = []

    def with_collection(self, name):
        self.events.append(("with_collection", name))
        if name not in self.registry:
            VersionedStore(self.events, name, self.registry)
        return self.registry[name]

    def init_collection(self, dense_dim):
        self.events.append(("base_init", self.collection_name, dense_dim))

    def init_versioned_collection(self, dense_dim):
        self.events.append(("versioned_init", self.collection_name, dense_dim))

    def insert(self, data):
        self.events.append(("versioned_insert", self.collection_name, data))
        if self.insert_responses:
            response = self.insert_responses.pop(0)
            if response.get("insert_count") == len(data):
                self.records.extend(data)
            return response
        self.records.extend(data)
        return {"insert_count": len(data)}

    def verify_version(self, **kwargs):
        self.events.append(("verify", self.collection_name, kwargs))
        return self.verify_result

    def delete_by_version(self, **kwargs):
        self.events.append(("delete_version", self.collection_name, kwargs))
        retained = []
        deleted = 0
        for record in self.records:
            if all(record.get(field) == value for field, value in kwargs.items()):
                deleted += 1
            else:
                retained.append(record)
        self.records = retained
        return deleted


class FailSecondEmbeddingOnce:
    def __init__(self):
        self.calls = 0

    def get_embeddings(self, texts):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("synthetic embedding failure")
        return [[float(self.calls)]] * len(texts)


def versioned_document(index, *, version="version-1", document="doc-1"):
    return {
        "text": f"text {index}",
        "filename": "doc.pdf",
        "file_type": "PDF",
        "chunk_id": f"{version}::doc.pdf::p1::l3::{index}",
        "tenant_id": "tenant-1",
        "knowledge_base_id": "kb-1",
        "document_id": document,
        "document_version_id": version,
        "section_id": "section-1",
        "acl_tags": ["team:legal", "team:legal", ""],
        "index_version": "index-v2",
        "content_hash": f"{index + 1:064x}",
        "chunk_level": 3,
        "chunk_idx": index,
    }


class MilvusWriterTests(unittest.TestCase):
    def test_versioned_write_defaults_to_isolated_catalog_collection_and_receipt(self):
        module = load_milvus_writer_module()
        events = []
        base_store = VersionedStore(events)
        writer = module.MilvusWriter(
            embedding_service=FakeEmbeddingService(events),
            milvus_manager=base_store,
        )

        receipt = writer.write_versioned_documents(
            [versioned_document(0), versioned_document(1), versioned_document(2)],
            batch_size=2,
        )

        self.assertEqual("documents_catalog_v1", receipt.collection_name)
        self.assertEqual("tenant-1", receipt.tenant_id)
        self.assertEqual("kb-1", receipt.knowledge_base_id)
        self.assertEqual("doc-1", receipt.document_id)
        self.assertEqual("version-1", receipt.document_version_id)
        self.assertEqual("index-v2", receipt.index_version)
        self.assertEqual(3, receipt.attempted_count)
        self.assertEqual(3, receipt.inserted_count)
        self.assertEqual(2, receipt.batch_count)
        self.assertEqual(
            (
                "version-1::doc.pdf::p1::l3::0",
                "version-1::doc.pdf::p1::l3::1",
                "version-1::doc.pdf::p1::l3::2",
            ),
            receipt.chunk_ids,
        )
        versioned_inserts = [
            event for event in events if event[0] == "versioned_insert"
        ]
        self.assertEqual(2, len(versioned_inserts))
        self.assertTrue(
            all(event[1] == "documents_catalog_v1" for event in versioned_inserts)
        )
        first_payload = versioned_inserts[0][2][0]
        self.assertEqual(["team:legal"], first_payload["acl_tags"])
        self.assertEqual("version-1", first_payload["document_version_id"])
        self.assertEqual("index-v2", first_payload["index_version"])
        self.assertNotIn(
            "documents",
            [event[1] for event in versioned_inserts],
        )
        cleanup_index = next(
            index for index, event in enumerate(events) if event[0] == "delete_version"
        )
        insert_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "versioned_insert"
        )
        self.assertLess(cleanup_index, insert_index)

    def test_versioned_write_rejects_mixed_scope_and_duplicate_ids(self):
        module = load_milvus_writer_module()
        events = []
        writer = module.MilvusWriter(
            embedding_service=FakeEmbeddingService(events),
            milvus_manager=VersionedStore(events),
        )
        mixed = [versioned_document(0), versioned_document(1, version="version-2")]
        with self.assertRaisesRegex(ValueError, "one version scope"):
            writer.write_versioned_documents(mixed)

        duplicate = [versioned_document(0), versioned_document(0)]
        with self.assertRaisesRegex(ValueError, "duplicate versioned chunk_id"):
            writer.write_versioned_documents(duplicate)

        self.assertFalse(any(event[0] == "versioned_init" for event in events))

    def test_versioned_write_rejects_embedding_and_insert_count_mismatches(self):
        module = load_milvus_writer_module()
        events = []
        writer = module.MilvusWriter(
            embedding_service=ShortEmbeddingService(),
            milvus_manager=VersionedStore(events),
        )
        with self.assertRaisesRegex(RuntimeError, "vector count"):
            writer.write_versioned_documents(
                [versioned_document(0), versioned_document(1)]
            )

        events = []
        store = VersionedStore(events)
        candidate = store.with_collection("documents_catalog_v1")
        candidate.insert_responses = [{"insert_count": 1}]
        writer = module.MilvusWriter(
            embedding_service=FakeEmbeddingService(events),
            milvus_manager=store,
        )
        with self.assertRaisesRegex(RuntimeError, "expected 2"):
            writer.write_versioned_documents(
                [versioned_document(0), versioned_document(1)]
            )

    def test_receipt_verify_and_delete_use_the_receipt_collection_and_scope(self):
        module = load_milvus_writer_module()
        events = []
        store = VersionedStore(events)
        writer = module.MilvusWriter(
            embedding_service=FakeEmbeddingService(events),
            milvus_manager=store,
        )
        receipt = writer.write_versioned_documents([versioned_document(0)])
        candidate = store.registry[receipt.collection_name]

        self.assertIs(candidate.verify_result, writer.verify_receipt(receipt))
        self.assertEqual(1, writer.delete_by_version(receipt))
        verify_event = next(event for event in events if event[0] == "verify")
        delete_event = [event for event in events if event[0] == "delete_version"][-1]
        self.assertEqual(receipt.collection_name, verify_event[1])
        self.assertEqual(receipt.chunk_ids, verify_event[2]["expected_chunk_ids"])
        self.assertEqual("version-1", delete_event[2]["document_version_id"])

    def test_partial_failure_retry_replaces_same_scope_without_duplicate_auto_ids(self):
        module = load_milvus_writer_module()
        events = []
        store = VersionedStore(events)
        embedding = FailSecondEmbeddingOnce()
        writer = module.MilvusWriter(
            embedding_service=embedding,
            milvus_manager=store,
        )
        documents = [versioned_document(0), versioned_document(1)]
        scope = writer.build_version_scope(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="index-v2",
        )

        with self.assertRaisesRegex(RuntimeError, "synthetic embedding failure"):
            writer.write_versioned_documents(documents, batch_size=1)
        candidate = store.registry[scope.collection_name]
        self.assertEqual(1, len(candidate.records))

        receipt = writer.write_versioned_documents(documents, batch_size=1)

        self.assertEqual(scope.collection_name, receipt.collection_name)
        self.assertEqual(2, len(candidate.records))
        self.assertEqual(
            receipt.chunk_ids,
            tuple(record["chunk_id"] for record in candidate.records),
        )

    def test_scope_can_cleanup_partial_candidate_when_no_receipt_exists(self):
        module = load_milvus_writer_module()
        events = []
        store = VersionedStore(events)
        writer = module.MilvusWriter(
            embedding_service=FailSecondEmbeddingOnce(),
            milvus_manager=store,
        )
        scope = writer.build_version_scope(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="index-v2",
        )

        with self.assertRaises(RuntimeError):
            writer.write_versioned_documents(
                [versioned_document(0), versioned_document(1)],
                batch_size=1,
            )
        candidate = store.registry[scope.collection_name]
        self.assertEqual(1, len(candidate.records))

        self.assertEqual(1, writer.delete_by_version(scope))
        self.assertEqual([], candidate.records)


if __name__ == "__main__":
    unittest.main()
