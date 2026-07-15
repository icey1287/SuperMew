import importlib.util
import sys
import unittest
from pathlib import Path

from pymilvus.exceptions import ParamError


MODULE_NAME = "milvus_client_shape_under_test"
SPEC = importlib.util.spec_from_file_location(
    MODULE_NAME,
    Path(__file__).resolve().parents[1] / "backend" / "indexing" / "milvus_client.py",
)
milvus = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = milvus
SPEC.loader.exec_module(milvus)


class MilvusRetrievalShapeTests(unittest.TestCase):
    def _store(self, response):
        store = milvus.MilvusStore(
            milvus.MilvusSettings(
                host="localhost",
                port="19530",
                collection_name="test_collection",
                uri="http://localhost:19530",
                timeout=1.0,
            )
        )
        store._run = lambda operation: response
        return store

    def _hybrid(self, store):
        return store.hybrid_retrieve(
            dense_embedding=[0.1, 0.2],
            query="query",
            top_k=1,
        )

    def _dense(self, store):
        return store.dense_retrieve(
            dense_embedding=[0.1, 0.2],
            top_k=1,
        )

    def test_healthy_empty_response_requires_one_empty_query_result(self):
        for invoke in (self._hybrid, self._dense):
            with self.subTest(invoke=invoke.__name__):
                self.assertEqual([], invoke(self._store([[]])))

    def test_malformed_outer_and_hit_shapes_raise_instead_of_becoming_empty(self):
        malformed = [
            {},
            [],
            [{}],
            [[{}]],
            [[{"id": 1, "distance": 0.2, "entity": "not-a-record"}]],
            [
                [
                    {
                        "id": 1,
                        "distance": float("nan"),
                        "entity": {"text": "x", "chunk_id": "c1"},
                    }
                ]
            ],
        ]
        for response in malformed:
            for invoke in (self._hybrid, self._dense):
                with self.subTest(response=response, invoke=invoke.__name__):
                    with self.assertRaises(ValueError):
                        invoke(self._store(response))

    def test_valid_hit_accepts_the_milvus_entity_envelope(self):
        response = [
            [
                {
                    "id": 7,
                    "distance": 0.85,
                    "entity": {
                        "text": "evidence",
                        "filename": "guide.md",
                        "chunk_id": "chunk-1",
                    },
                }
            ]
        ]

        for invoke in (self._hybrid, self._dense):
            with self.subTest(invoke=invoke.__name__):
                self.assertEqual(
                    {
                        "id": 7,
                        "text": "evidence",
                        "filename": "guide.md",
                        "file_type": "",
                        "page_number": 0,
                        "chunk_id": "chunk-1",
                        "parent_chunk_id": "",
                        "root_chunk_id": "",
                        "chunk_level": 0,
                        "chunk_idx": 0,
                        "score": 0.85,
                    },
                    invoke(self._store(response))[0],
                )

    def test_generic_param_error_is_not_misclassified_as_hybrid_capability(self):
        store = self._store([[]])

        def fail(_operation):
            raise ParamError(message="invalid request parameters")

        store._run = fail
        with self.assertRaises(ParamError):
            self._hybrid(store)
        self.assertFalse(
            milvus._is_hybrid_capability_error(
                ParamError(message="invalid request parameters")
            )
        )


if __name__ == "__main__":
    unittest.main()
