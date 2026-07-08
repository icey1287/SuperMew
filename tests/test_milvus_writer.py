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


class FakeMilvusStore:
    collection_name = "test_collection"

    def __init__(self, events):
        self.events = events

    def init_collection(self, dense_dim):
        self.events.append(("init_collection", dense_dim))

    def insert(self, data):
        self.events.append(("insert", [item["chunk_id"] for item in data]))

    def session(self):
        raise AssertionError("write_documents must not hold a Milvus session while embedding")


class MilvusWriterTests(unittest.TestCase):
    def test_write_documents_opens_short_insert_calls_after_embedding_batches(self):
        module = load_milvus_writer_module()
        events = []
        writer = module.MilvusWriter(
            embedding_service=FakeEmbeddingService(events),
            milvus_manager=FakeMilvusStore(events),
        )
        documents = [
            {
                "text": f"text {idx}",
                "filename": "doc.pdf",
                "file_type": "PDF",
                "chunk_id": f"chunk-{idx}",
            }
            for idx in range(3)
        ]

        progress = []
        writer.write_documents(
            documents,
            batch_size=2,
            progress_callback=lambda processed, total: progress.append((processed, total)),
        )

        self.assertEqual(
            events,
            [
                ("init_collection", 1024),
                ("embed", ["text 0", "text 1"]),
                ("insert", ["chunk-0", "chunk-1"]),
                ("embed", ["text 2"]),
                ("insert", ["chunk-2"]),
            ],
        )
        self.assertEqual(progress, [(2, 3), (3, 3)])


if __name__ == "__main__":
    unittest.main()
