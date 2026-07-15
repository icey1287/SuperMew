import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeEmbeddingService:
    def __init__(self):
        self.calls = 0

    def get_embeddings(self, texts):
        self.calls += 1
        return [[0.1, 0.2]]


class FakeMilvusStore:
    hybrid_error_type = RuntimeError

    def hybrid_retrieve(self, **kwargs):
        raise self.hybrid_error_type("hybrid unavailable")

    def dense_retrieve(self, **kwargs):
        return [
            {
                "text": "fallback result",
                "filename": "doc.md",
                "page_number": 1,
                "chunk_id": "chunk-1",
                "score": 0.9,
            }
        ]


class FakeRerankStage:
    def __init__(self, *, enabled: bool):
        self.enabled = enabled

    def run(self, query, docs, top_k, **kwargs):
        del query, kwargs
        selected = [
            {**doc, "rrf_rank": index} for index, doc in enumerate(docs[:top_k], 1)
        ]
        return selected, {
            "rerank_enabled": self.enabled,
            "rerank_applied": False,
            "rerank_model": "fake-reranker" if self.enabled else None,
            "rerank_error_code": None,
            "rerank_retryable": None,
            "rerank_attempts": 0,
            "rerank_fallback_applied": False,
            "rerank_timeout_seconds": 5.0 if self.enabled else 0.0,
            "rerank_min_score": 0.0,
            "rerank_threshold_applied": False,
            "rerank_skip_reason": None if self.enabled else "disabled",
            "candidate_count": len(docs),
            "post_rerank_count": len(selected),
            "post_threshold_count": len(selected),
        }


def load_utils(env):
    embedding_service = FakeEmbeddingService()
    milvus_store = FakeMilvusStore()

    class HybridRetrievalUnsupported(RuntimeError):
        pass

    milvus_store.hybrid_error_type = HybridRetrievalUnsupported

    fake_indexing = types.ModuleType("backend.indexing")
    fake_indexing.__path__ = []

    fake_milvus = types.ModuleType("backend.indexing.milvus_client")
    fake_milvus.HybridRetrievalUnsupported = HybridRetrievalUnsupported
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

    rerank_values = [
        str(env.get("RERANK_MODEL") or ""),
        str(env.get("RERANK_BINDING_HOST") or ""),
        str(env.get("RERANK_API_KEY") or ""),
    ]
    rerank_enabled = all(
        value
        and not value.lower().startswith(("your_", "your-", "replace-with"))
        and "your-rerank" not in value.lower()
        and "your_rerank" not in value.lower()
        for value in rerank_values
    )
    module._rerank_stage = FakeRerankStage(enabled=rerank_enabled)

    return module, embedding_service


class RagLatencyGuardTests(unittest.TestCase):
    def test_rewrite_model_disables_sdk_retries_and_sets_native_timeout(self):
        utils, _ = load_utils(
            {
                "ARK_API_KEY": "test-key",
                "FAST_MODEL": "fast-model",
                "BASE_URL": "https://example.test/v1",
                "RAG_MODEL_TIMEOUT_SECONDS": "7.5",
                "AUTO_MERGE_ENABLED": "false",
            }
        )
        initialized = Mock(return_value=object())
        utils.init_chat_model = initialized
        utils._rewrite_model = None

        self.assertIsNotNone(utils._get_rewrite_model())
        initialized.assert_called_once_with(
            model="fast-model",
            model_provider="openai",
            api_key="test-key",
            base_url="https://example.test/v1",
            temperature=0,
            stream_usage=True,
            max_retries=0,
            timeout=7.5,
        )

    def test_placeholder_rerank_settings_are_treated_as_disabled(self):
        utils, _ = load_utils(
            {
                "RERANK_MODEL": "your_rerank_model",
                "RERANK_BINDING_HOST": "https://your-rerank-host",
                "RERANK_API_KEY": "your_rerank_api_key",
                "AUTO_MERGE_ENABLED": "false",
            }
        )

        docs, meta = utils._rerank_documents(
            "query",
            [{"text": "doc", "chunk_id": "chunk-1", "score": 0.9}],
            1,
        )

        self.assertFalse(meta["rerank_enabled"])
        self.assertEqual(1, len(docs))

    def test_dense_fallback_reuses_the_query_embedding(self):
        utils, embedding_service = load_utils(
            {
                "RERANK_MODEL": "",
                "RERANK_BINDING_HOST": "",
                "RERANK_API_KEY": "",
                "AUTO_MERGE_ENABLED": "false",
            }
        )

        result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual(1, embedding_service.calls)
        self.assertEqual("dense_fallback", result["meta"]["retrieval_mode"])
        self.assertEqual(1, len(result["docs"]))

    def test_retrieval_uses_query_embedding_semantics_when_available(self):
        utils, _ = load_utils(
            {
                "RERANK_MODEL": "",
                "RERANK_BINDING_HOST": "",
                "RERANK_API_KEY": "",
                "AUTO_MERGE_ENABLED": "false",
            }
        )

        class SemanticEmbedding:
            def __init__(self):
                self.calls = []

            def embed_query(self, text, **kwargs):
                self.calls.append((text, kwargs))
                return [0.1, 0.2]

            def get_embeddings(self, texts):
                raise AssertionError(f"query must not use document semantics: {texts}")

        embedding = SemanticEmbedding()
        utils._embedding_service = embedding

        result = utils.retrieve_documents("query text", top_k=1)

        self.assertEqual(1, len(embedding.calls))
        self.assertEqual("query text", embedding.calls[0][0])
        self.assertIn("scope", embedding.calls[0][1])
        self.assertEqual("dense_fallback", result["meta"]["retrieval_mode"])

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
            (
                {
                    "method": "step_back",
                    "step_back_question": "更抽象的问题是什么？",
                    "hyde_document": "",
                },
                "step_back",
                "退步问题",
            ),
            (
                {
                    "method": "hyde",
                    "step_back_question": "",
                    "hyde_document": "一段可能的答案式文档",
                },
                "hyde",
                "假设性答案文档",
            ),
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
