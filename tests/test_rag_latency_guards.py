import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeEmbeddingService:
    def __init__(self):
        self.calls = 0

    def get_embeddings(self, texts):
        self.calls += 1
        return [[0.1, 0.2]]


class FakeMilvusStore:
    def hybrid_retrieve(self, **kwargs):
        raise RuntimeError("hybrid unavailable")

    def dense_retrieve(self, **kwargs):
        return [{
            "text": "fallback result",
            "filename": "doc.md",
            "page_number": 1,
            "chunk_id": "chunk-1",
            "score": 0.9,
        }]


def load_utils(env):
    embedding_service = FakeEmbeddingService()
    milvus_store = FakeMilvusStore()

    fake_indexing = types.ModuleType("backend.indexing")
    fake_indexing.__path__ = []

    fake_milvus = types.ModuleType("backend.indexing.milvus_client")
    fake_milvus.get_milvus_store = lambda: milvus_store

    fake_embedding = types.ModuleType("backend.indexing.embedding")
    fake_embedding.embedding_service = embedding_service

    fake_parent_store = types.ModuleType("backend.indexing.parent_chunk_store")

    class ParentChunkStore:
        def get_documents_by_ids(self, chunk_ids):
            return []

    fake_parent_store.ParentChunkStore = ParentChunkStore

    module_name = f"rag_utils_under_test_{id(embedding_service)}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / "backend" / "rag" / "utils.py",
    )
    module = importlib.util.module_from_spec(spec)

    with (
        patch.dict(os.environ, env, clear=False),
        patch.dict(
            sys.modules,
            {
                "backend.indexing": fake_indexing,
                "backend.indexing.milvus_client": fake_milvus,
                "backend.indexing.embedding": fake_embedding,
                "backend.indexing.parent_chunk_store": fake_parent_store,
            },
        ),
    ):
        spec.loader.exec_module(module)

    return module, embedding_service


class RagLatencyGuardTests(unittest.TestCase):
    def test_placeholder_rerank_settings_are_treated_as_disabled(self):
        utils, _ = load_utils({
            "RERANK_MODEL": "your_rerank_model",
            "RERANK_BINDING_HOST": "https://your-rerank-host",
            "RERANK_API_KEY": "your_rerank_api_key",
            "AUTO_MERGE_ENABLED": "false",
        })

        with patch.object(utils.requests, "post") as post:
            docs, meta = utils._rerank_documents(
                "query",
                [{"text": "doc", "chunk_id": "chunk-1", "score": 0.9}],
                1,
            )

        self.assertFalse(utils.RERANK_ENABLED)
        self.assertFalse(meta["rerank_enabled"])
        self.assertEqual(1, len(docs))
        post.assert_not_called()

    def test_dense_fallback_reuses_the_query_embedding(self):
        utils, embedding_service = load_utils({
            "RERANK_MODEL": "",
            "RERANK_BINDING_HOST": "",
            "RERANK_API_KEY": "",
            "AUTO_MERGE_ENABLED": "false",
        })

        result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual(1, embedding_service.calls)
        self.assertEqual("dense_fallback", result["meta"]["retrieval_mode"])
        self.assertEqual(1, len(result["docs"]))

    def test_rewrite_single_choice_uses_one_model_call(self):
        utils, _ = load_utils({"AUTO_MERGE_ENABLED": "false"})

        class Model:
            def __init__(self, payload):
                self.calls = 0
                self.payload = payload
                self.schema = None

            def with_structured_output(self, schema):
                self.schema = schema
                return self

            def invoke(self, messages):
                self.calls += 1
                return self.schema(**self.payload)

        cases = [
            ({
                "method": "step_back",
                "step_back_question": "更抽象的问题是什么？",
                "hyde_document": "",
            }, "step_back", "退步问题"),
            ({
                "method": "hyde",
                "step_back_question": "",
                "hyde_document": "一段可能的答案式文档",
            }, "hyde", "假设性答案文档"),
        ]
        for payload, expected_method, expected_marker in cases:
            with self.subTest(method=expected_method):
                model = Model(payload)
                utils._get_rewrite_model = lambda: model

                result = utils.rewrite_query_once("具体问题")

                self.assertEqual(1, model.calls)
                self.assertEqual(expected_method, result["rewrite_method"])
                self.assertIn(expected_marker, result["rewritten_query"])


if __name__ == "__main__":
    unittest.main()
