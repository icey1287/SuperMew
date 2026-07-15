import unittest
from unittest.mock import patch

from backend.providers import ProviderCode, ProviderError, ProviderExecutor
from test_rag_latency_guards import load_utils


class _BrokenEmbedding:
    def __init__(self):
        self.calls = 0

    def get_embeddings(self, texts):
        self.calls += 1
        raise ConnectionError("secret embedding endpoint and token")


class _HealthyEmbedding:
    def get_embeddings(self, texts):
        return [[0.1, 0.2]]


class _NeverCalledStore:
    def __init__(self):
        self.calls = 0

    def hybrid_retrieve(self, **kwargs):
        self.calls += 1
        return []


class _RerankStageStub:
    def __init__(
        self,
        *,
        enabled=True,
        error_code=None,
        retryable=None,
        attempts=0,
    ):
        self.enabled = enabled
        self.error_code = error_code
        self.retryable = retryable
        self.attempts = attempts

    def run(self, query, docs, top_k, **kwargs):
        del query, kwargs
        ranked = [{**doc, "rrf_rank": index} for index, doc in enumerate(docs, 1)]
        selected = ranked[:top_k]
        return selected, {
            "rerank_enabled": self.enabled,
            "rerank_applied": False,
            "rerank_model": "rerank-model" if self.enabled else None,
            "rerank_error_code": self.error_code,
            "rerank_retryable": self.retryable,
            "rerank_attempts": self.attempts,
            "rerank_fallback_applied": self.error_code is not None,
            "rerank_timeout_seconds": 5.0 if self.enabled else 0.0,
            "rerank_min_score": 0.5,
            "rerank_threshold_applied": False,
            "rerank_skip_reason": None if self.enabled else "disabled",
            "candidate_count": len(ranked),
            "post_rerank_count": len(selected),
            "post_threshold_count": len(selected),
        }


class RagFaultInjectionTests(unittest.TestCase):
    def _utils(self, **env):
        defaults = {
            "RERANK_MODEL": "",
            "RERANK_BINDING_HOST": "",
            "RERANK_API_KEY": "",
            "AUTO_MERGE_ENABLED": "false",
        }
        defaults.update(env)
        utils, _ = load_utils(defaults)
        utils._provider_executor = ProviderExecutor(sleeper=lambda _: None)
        return utils

    def test_embedding_failure_is_typed_and_never_becomes_empty_retrieval(self):
        utils = self._utils()
        embedding = _BrokenEmbedding()
        store = _NeverCalledStore()
        utils._embedding_service = embedding
        utils._milvus_manager = store

        with self.assertRaises(ProviderError) as raised:
            utils.retrieve_documents("query", top_k=1)

        self.assertEqual(ProviderCode.EMBEDDING_UNAVAILABLE, raised.exception.code)
        self.assertEqual(1, embedding.calls)
        self.assertEqual(0, store.calls)
        self.assertNotIn("secret", raised.exception.message)
        self.assertNotIn("secret", str(raised.exception.safe_details))

    def test_vector_failure_does_not_trigger_dense_fallback_or_no_knowledge(self):
        utils = self._utils()

        class Store:
            def __init__(self):
                self.hybrid_calls = 0
                self.dense_calls = 0

            def hybrid_retrieve(self, **kwargs):
                self.hybrid_calls += 1
                raise ConnectionError("secret milvus address")

            def dense_retrieve(self, **kwargs):
                self.dense_calls += 1
                return []

        store = Store()
        utils._embedding_service = _HealthyEmbedding()
        utils._milvus_manager = store

        with self.assertRaises(ProviderError) as raised:
            utils.retrieve_documents("query", top_k=1)

        self.assertEqual(ProviderCode.VECTOR_STORE_UNAVAILABLE, raised.exception.code)
        self.assertEqual(2, store.hybrid_calls)
        self.assertEqual(0, store.dense_calls)

    def test_malformed_vector_response_is_typed_provider_failure(self):
        utils = self._utils()

        class Store:
            def hybrid_retrieve(self, **kwargs):
                return None

        utils._embedding_service = _HealthyEmbedding()
        utils._milvus_manager = Store()

        with self.assertRaises(ProviderError) as raised:
            utils.retrieve_documents("query", top_k=1)

        self.assertEqual(ProviderCode.VECTOR_STORE_UNAVAILABLE, raised.exception.code)

    def test_healthy_empty_vector_result_is_the_only_empty_retrieval_outcome(self):
        utils = self._utils()

        class Store:
            def hybrid_retrieve(self, **kwargs):
                return []

            def dense_retrieve(self, **kwargs):
                raise AssertionError("dense fallback is not expected")

        utils._embedding_service = _HealthyEmbedding()
        utils._milvus_manager = Store()

        result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual([], result["docs"])
        self.assertTrue(result["meta"]["retrieval_empty"])
        self.assertEqual("hybrid", result["meta"]["retrieval_mode"])

    def test_rerank_timeout_falls_back_without_exposing_upstream_details(self):
        utils = self._utils(
            RERANK_MODEL="rerank-model",
            RERANK_BINDING_HOST="https://internal.example.test/secret",
            RERANK_API_KEY="top-secret-key",
        )
        docs = [
            {"text": "first", "chunk_id": "c1", "score": 0.9},
            {"text": "second", "chunk_id": "c2", "score": 0.8},
        ]

        utils._rerank_stage = _RerankStageStub(
            error_code="RERANK_TIMEOUT",
            retryable=True,
            attempts=2,
        )
        reranked, meta = utils._rerank_documents("query", docs, 2)

        self.assertEqual(["c1", "c2"], [item["chunk_id"] for item in reranked])
        self.assertEqual("RERANK_TIMEOUT", meta["rerank_error_code"])
        self.assertFalse(meta["rerank_applied"])
        self.assertTrue(meta["rerank_fallback_applied"])
        self.assertEqual(2, meta["rerank_attempts"])
        serialized = str(meta)
        self.assertNotIn("internal.example.test", serialized)
        self.assertNotIn("top-secret-key", serialized)
        self.assertNotIn("rerank_endpoint", meta)

    def test_rerank_http_body_is_not_copied_to_trace(self):
        utils = self._utils(
            RERANK_MODEL="rerank-model",
            RERANK_BINDING_HOST="https://rerank.example.test",
            RERANK_API_KEY="top-secret-key",
        )
        utils._rerank_stage = _RerankStageStub(
            error_code="RERANK_UNAVAILABLE",
            retryable=True,
            attempts=2,
        )
        _, meta = utils._rerank_documents(
            "query",
            [{"text": "doc", "chunk_id": "c1", "score": 0.9}],
            1,
        )

        self.assertEqual("RERANK_UNAVAILABLE", meta["rerank_error_code"])
        self.assertNotIn("raw-secret-upstream-body", str(meta))

    def test_rerank_missing_score_falls_back_instead_of_creating_false_empty(self):
        utils = self._utils(
            RERANK_MODEL="rerank-model",
            RERANK_BINDING_HOST="https://rerank.example.test",
            RERANK_API_KEY="secret-key",
            RERANK_MIN_SCORE="0.5",
        )
        utils._embedding_service = _HealthyEmbedding()

        class Store:
            def hybrid_retrieve(self, **kwargs):
                return [{"text": "evidence", "chunk_id": "c1", "score": 0.1}]

        utils._milvus_manager = Store()
        utils._rerank_stage = _RerankStageStub(
            error_code="RERANK_INVALID_RESPONSE",
            retryable=False,
            attempts=1,
        )
        result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual(1, len(result["docs"]))
        self.assertEqual("RERANK_INVALID_RESPONSE", result["meta"]["rerank_error_code"])
        self.assertTrue(result["meta"]["rerank_fallback_applied"])
        self.assertFalse(result["meta"]["rerank_threshold_applied"])

    def test_rerank_fallback_does_not_apply_rerank_threshold_to_recall_score(self):
        utils = self._utils(
            RERANK_MODEL="rerank-model",
            RERANK_BINDING_HOST="https://rerank.example.test",
            RERANK_API_KEY="secret-key",
            RERANK_MIN_SCORE="0.5",
        )
        utils._embedding_service = _HealthyEmbedding()

        class Store:
            def hybrid_retrieve(self, **kwargs):
                return [{"text": "evidence", "chunk_id": "c1", "score": 0.1}]

        utils._milvus_manager = Store()
        utils._rerank_stage = _RerankStageStub(
            error_code="RERANK_TIMEOUT",
            retryable=True,
            attempts=2,
        )
        result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual(1, len(result["docs"]))
        self.assertFalse(result["meta"]["retrieval_empty"])
        self.assertFalse(result["meta"]["rerank_threshold_applied"])
        self.assertEqual("RERANK_TIMEOUT", result["meta"]["rerank_error_code"])

    def test_disabled_rerank_does_not_apply_rerank_threshold_to_recall_score(self):
        utils = self._utils(RERANK_MIN_SCORE="0.5")
        utils._embedding_service = _HealthyEmbedding()

        class Store:
            def hybrid_retrieve(self, **kwargs):
                return [{"text": "evidence", "chunk_id": "c1", "score": 0.1}]

        utils._milvus_manager = Store()
        utils._rerank_stage = _RerankStageStub(enabled=False)
        result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual(1, len(result["docs"]))
        self.assertFalse(result["meta"]["retrieval_empty"])
        self.assertFalse(result["meta"]["rerank_threshold_applied"])

    def test_dense_fallback_reuses_the_same_vector_stage_deadline(self):
        utils = self._utils()

        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = Clock()
        dense_timeouts = []

        class Store:
            def hybrid_retrieve(self, **kwargs):
                clock.now = 9.0
                raise utils.HybridRetrievalUnsupported()

            def dense_retrieve(self, **kwargs):
                dense_timeouts.append(kwargs["timeout"])
                return []

        utils._embedding_service = _HealthyEmbedding()
        utils._milvus_manager = Store()
        utils._provider_executor = ProviderExecutor(
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        with patch.object(utils.time, "monotonic", clock.monotonic):
            result = utils.retrieve_documents("query", top_k=1)

        self.assertEqual([], result["docs"])
        self.assertEqual(1, len(dense_timeouts))
        self.assertLessEqual(dense_timeouts[0], 1.0)


if __name__ == "__main__":
    unittest.main()
