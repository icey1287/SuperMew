import unittest

from pydantic import ValidationError

from backend.schemas.chat import HitlResumeState, RagTrace, normalize_rag_trace


class RagTraceSchemaTests(unittest.TestCase):
    def test_trace_schema_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            RagTrace.model_validate({"query": "q", "unsupported_field": True})

    def test_trace_normalizer_removes_unknown_top_level_and_nested_fields(self):
        trace = normalize_rag_trace({
            "query": "main",
            "rewrite_method": "hyde",
            "hyde_document": "用于检索的假设性答案",
            "unsupported_field": True,
            "sub_traces": [{
                "query": "sub",
                "route": "answer",
                "unsupported_nested_field": True,
            }],
        })

        self.assertEqual("main", trace["query"])
        self.assertEqual("hyde", trace["rewrite_method"])
        self.assertIn("假设性答案", trace["hyde_document"])
        self.assertNotIn("unsupported_field", trace)
        self.assertEqual([{"query": "sub", "route": "answer"}], trace["sub_traces"])

    def test_resume_state_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            HitlResumeState.model_validate({
                "question": "问题",
                "route": "clarify",
                "retrieval_status": "needs_clarification",
                "unsupported_field": True,
            })


if __name__ == "__main__":
    unittest.main()
