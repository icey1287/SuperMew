import unittest

from pydantic import ValidationError

from backend.schemas.chat import HitlResumeState, RagTrace, normalize_rag_trace


class RagTraceSchemaTests(unittest.TestCase):
    def test_trace_schema_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            RagTrace.model_validate({"query": "q", "unsupported_field": True})

    def test_trace_normalizer_removes_unknown_top_level_and_nested_fields(self):
        trace = normalize_rag_trace(
            {
                "query": "main",
                "rewrite_method": "hyde",
                "hyde_document": "用于检索的假设性答案",
                "unsupported_field": True,
                "sub_traces": [
                    {
                        "query": "sub",
                        "route": "answer",
                        "unsupported_nested_field": True,
                    }
                ],
            }
        )

        self.assertEqual("main", trace["query"])
        self.assertEqual("hyde", trace["rewrite_method"])
        self.assertIn("假设性答案", trace["hyde_document"])
        self.assertNotIn("unsupported_field", trace)
        self.assertEqual([{"query": "sub", "route": "answer"}], trace["sub_traces"])

    def test_resume_state_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            HitlResumeState.model_validate(
                {
                    "question": "问题",
                    "route": "clarify",
                    "retrieval_status": "needs_clarification",
                    "unsupported_field": True,
                }
            )

    def test_retrieval_target_trace_survives_normalization_without_unknown_data(self):
        trace = normalize_rag_trace(
            {
                "retrieval_index_id": "a" * 64,
                "retrieval_target_count": 2,
                "retrieval_required_target_count": 1,
                "retrieval_optional_target_count": 1,
                "retrieval_optional_missing_count": 1,
                "deduplicated_recall_count": 3,
                "retrieval_target_results": [
                    {
                        "collection_name": "embeddings_collection_catalog_v1",
                        "storage_layout": "versioned",
                        "required": True,
                        "mode": "hybrid",
                        "hit_count": 3,
                        "raw_filter": "must-not-persist",
                    },
                    {
                        "collection_name": "embeddings_collection",
                        "storage_layout": "legacy_filename",
                        "required": False,
                        "mode": "missing_optional",
                        "hit_count": 0,
                    },
                ],
            }
        )

        self.assertEqual("a" * 64, trace["retrieval_index_id"])
        self.assertEqual(3, trace["deduplicated_recall_count"])
        self.assertNotIn("raw_filter", trace["retrieval_target_results"][0])
        self.assertEqual(0, trace["retrieval_target_results"][1]["hit_count"])

    def test_retrieved_chunk_keeps_versioned_manifest_identity(self):
        trace = normalize_rag_trace(
            {
                "retrieved_chunks": [
                    {
                        "filename": "guide.pdf",
                        "file_type": "PDF",
                        "page_number": 3,
                        "text": "evidence",
                        "chunk_id": "version-v2::guide.pdf::p3::l3::0",
                        "parent_chunk_id": "version-v2::guide.pdf::p3::l2::0",
                        "root_chunk_id": "version-v2::guide.pdf::p3::l1::0",
                        "chunk_level": 3,
                        "chunk_idx": 7,
                        "document_id": "doc-1",
                        "document_version_id": "version-v2",
                        "section_id": "page:3",
                        "index_version": "catalog-v1",
                        "content_hash": "b" * 64,
                        "merged_from_children": False,
                        "private_metadata": "must-not-persist",
                    }
                ]
            }
        )

        chunk = trace["retrieved_chunks"][0]
        self.assertEqual("doc-1", chunk["document_id"])
        self.assertEqual("version-v2", chunk["document_version_id"])
        self.assertEqual("b" * 64, chunk["content_hash"])
        self.assertEqual(3, chunk["chunk_level"])
        self.assertNotIn("private_metadata", chunk)

    def test_legacy_chunk_drops_empty_hash_but_versioned_chunk_fails_closed(self):
        legacy = normalize_rag_trace(
            {
                "retrieved_chunks": [
                    {
                        "filename": "legacy.pdf",
                        "chunk_id": "legacy::chunk",
                        "content_hash": "",
                    }
                ]
            }
        )
        self.assertNotIn("content_hash", legacy["retrieved_chunks"][0])

        with self.assertRaises(ValidationError):
            normalize_rag_trace(
                {
                    "retrieved_chunks": [
                        {
                            "filename": "guide.pdf",
                            "chunk_id": "version::chunk",
                            "document_version_id": "version-v2",
                            "content_hash": "",
                        }
                    ]
                }
            )
        with self.assertRaises(ValidationError):
            normalize_rag_trace(
                {
                    "retrieved_chunks": [
                        {
                            "filename": "guide.pdf",
                            "chunk_id": "version::chunk",
                            "document_version_id": "version-v2",
                        }
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
