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
                        "tenant_id": "default",
                        "knowledge_base_id": "",
                        "document_id": "",
                        "document_version_id": "",
                        "section_id": "",
                        "acl_tags": [],
                        "index_version": "v1",
                        "content_hash": None,
                        "score": 0.85,
                    },
                    invoke(self._store(response))[0],
                )

    def test_versioned_hit_returns_catalog_metadata(self):
        response = [
            [
                {
                    "id": 8,
                    "distance": 0.9,
                    "entity": {
                        "text": "versioned evidence",
                        "filename": "guide.md",
                        "chunk_id": "version-1::guide.md::p1::l3::0",
                        "tenant_id": "tenant-1",
                        "knowledge_base_id": "kb-1",
                        "document_id": "doc-1",
                        "document_version_id": "version-1",
                        "section_id": "section-1",
                        "acl_tags": ["team:legal"],
                        "index_version": "index-v2",
                        "content_hash": "a" * 64,
                    },
                }
            ]
        ]

        hit = self._dense(self._store(response))[0]

        self.assertEqual("tenant-1", hit["tenant_id"])
        self.assertEqual("kb-1", hit["knowledge_base_id"])
        self.assertEqual("doc-1", hit["document_id"])
        self.assertEqual("version-1", hit["document_version_id"])
        self.assertEqual("section-1", hit["section_id"])
        self.assertEqual(["team:legal"], hit["acl_tags"])
        self.assertEqual("index-v2", hit["index_version"])
        self.assertEqual("a" * 64, hit["content_hash"])

    def test_versioned_hit_rejects_missing_or_invalid_content_hash(self):
        for content_hash in (None, "", "not-a-sha256"):
            response = [
                [
                    {
                        "id": 8,
                        "distance": 0.9,
                        "entity": {
                            "text": "versioned evidence",
                            "filename": "guide.md",
                            "chunk_id": "version-1::guide.md::p1::l3::0",
                            "document_version_id": "version-1",
                            "content_hash": content_hash,
                        },
                    }
                ]
            ]

            with self.subTest(content_hash=content_hash):
                with self.assertRaises(ValueError):
                    self._dense(self._store(response))

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


class MilvusVersionPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.store = milvus.MilvusStore(
            milvus.MilvusSettings(
                host="localhost",
                port="19530",
                collection_name="base_catalog_v1",
                uri="http://localhost:19530",
                timeout=1.0,
            )
        )

    def test_with_collection_preserves_settings_without_mutating_base_store(self):
        candidate = self.store.with_collection("candidate_v2")

        self.assertEqual("base_catalog_v1", self.store.collection_name)
        self.assertEqual("candidate_v2", candidate.collection_name)
        self.assertEqual(self.store._settings.uri, candidate._settings.uri)

    def test_versioned_collection_schema_declares_catalog_metadata_fields(self):
        class Schema:
            def __init__(self):
                self.fields = []

            def add_field(self, name, *_args, **_kwargs):
                self.fields.append(name)

            def add_function(self, _function):
                return None

        class IndexParams:
            def add_index(self, **_kwargs):
                return None

        class Client:
            def __init__(self):
                self.schema = Schema()
                self.created = None

            def has_collection(self, _name):
                return False

            def create_schema(self, **_kwargs):
                return self.schema

            def prepare_index_params(self):
                return IndexParams()

            def create_collection(self, **kwargs):
                self.created = kwargs

        client = Client()

        self.store.ensure_collection(
            client,
            "candidate_v2",
            1024,
            include_catalog_fields=True,
        )

        for field in milvus.CATALOG_METADATA_FIELDS:
            self.assertIn(field, client.schema.fields)
        self.assertEqual("candidate_v2", client.created["collection_name"])

    def test_existing_versioned_collection_must_have_explicit_schema(self):
        class Client:
            def has_collection(self, _name):
                return True

            def describe_collection(self, _name):
                return {"fields": [{"name": "id"}, {"name": "chunk_id"}]}

        with self.assertRaisesRegex(RuntimeError, "missing fields"):
            self.store.ensure_collection(
                Client(),
                "candidate_v2",
                1024,
                include_catalog_fields=True,
            )

    def test_query_version_chunk_ids_uses_safe_exact_scope(self):
        captured = {}

        def query_all(filter_expr="", output_fields=None):
            captured["filter"] = filter_expr
            captured["fields"] = output_fields
            return [{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-2"}]

        self.store.query_all = query_all

        chunk_ids = self.store.query_version_chunk_ids(
            tenant_id='tenant" or id >= 0',
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="index-v2",
        )

        self.assertEqual(["chunk-1", "chunk-2"], chunk_ids)
        self.assertEqual(["chunk_id"], captured["fields"])
        self.assertIn('tenant_id == "tenant\\" or id >= 0"', captured["filter"])
        self.assertIn('document_version_id in ["version-1"]', captured["filter"])

    def test_verify_version_detects_set_count_and_duplicate_mismatches(self):
        self.store.query_version_chunk_ids = lambda **_kwargs: [
            "chunk-1",
            "chunk-2",
            "chunk-2",
            "unexpected",
        ]

        result = self.store.verify_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="index-v2",
            expected_chunk_ids=["chunk-1", "chunk-2", "missing"],
        )

        self.assertFalse(result.exact)
        self.assertEqual(("missing",), result.missing_ids)
        self.assertEqual(("unexpected",), result.unexpected_ids)
        self.assertEqual(("chunk-2",), result.duplicate_ids)
        self.assertEqual(3, result.expected_count)
        self.assertEqual(4, result.actual_count)

    def test_verify_version_is_exact_only_for_unique_equal_ids_and_count(self):
        self.store.query_version_chunk_ids = lambda **_kwargs: [
            "chunk-2",
            "chunk-1",
        ]

        result = self.store.verify_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            expected_chunk_ids=["chunk-1", "chunk-2"],
        )

        self.assertTrue(result.exact)
        self.assertEqual(
            2,
            self.store.count_by_version(
                tenant_id="tenant-1",
                knowledge_base_id="kb-1",
                document_id="doc-1",
                document_version_id="version-1",
            ),
        )

    def test_delete_by_version_returns_count_and_keeps_scope_isolated(self):
        captured = {}

        def delete(expression):
            captured["filter"] = expression
            return {"delete_count": 3}

        self.store.delete = delete
        deleted = self.store.delete_by_version(
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_version_id="version-1",
            index_version="index-v2",
        )

        self.assertEqual(3, deleted)
        self.assertIn('tenant_id == "tenant-1"', captured["filter"])
        self.assertIn('knowledge_base_id == "kb-1"', captured["filter"])
        self.assertIn('document_id == "doc-1"', captured["filter"])
        self.assertIn('document_version_id in ["version-1"]', captured["filter"])
        self.assertIn('index_version == "index-v2"', captured["filter"])

    def test_version_query_rejects_malformed_rows(self):
        self.store.query_all = lambda *_args, **_kwargs: [{"not_chunk_id": "x"}]

        with self.assertRaises(ValueError):
            self.store.query_version_chunk_ids(
                tenant_id="tenant-1",
                knowledge_base_id="kb-1",
                document_id="doc-1",
                document_version_id="version-1",
            )


if __name__ == "__main__":
    unittest.main()
