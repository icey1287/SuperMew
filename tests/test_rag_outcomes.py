import unittest

from backend.rag.outcomes import (
    RetrievalOutcome,
    outcome_for_result,
    partial_evidence_instruction,
    retrieval_user_message,
)
from backend.runs.agent_executor import _resume_answer_prompt


class RagOutcomePresentationTests(unittest.TestCase):
    def test_empty_insufficient_result_never_claims_no_knowledge(self):
        result = {
            "docs": [],
            "retrieval_status": "insufficient_evidence",
            "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
            "rag_trace": {
                "coverage_gap_codes": ["VECTOR_STORE_UNAVAILABLE"],
                "coverage_gap_questions": ["unknown sub"],
            },
        }

        self.assertEqual(
            RetrievalOutcome.INSUFFICIENT_EVIDENCE,
            outcome_for_result(result),
        )
        message = retrieval_user_message(result)
        self.assertIn("证据不足", message)
        self.assertIn("VECTOR_STORE_UNAVAILABLE", message)
        self.assertNotIn("知识库中没有找到", message)
        self.assertIsNone(_resume_answer_prompt(result, "补充"))

    def test_partial_docs_add_coverage_constraints_to_resume_prompt(self):
        result = {
            "question": "比较两个方案",
            "docs": [
                {
                    "filename": "guide.md",
                    "page_number": 1,
                    "text": "方案 A 的证据",
                }
            ],
            "retrieval_status": "partial",
            "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
            "rag_trace": {
                "coverage_gap_questions": ["方案 B 的限制"],
            },
        }

        instruction = partial_evidence_instruction(result)
        prompt = _resume_answer_prompt(result, "继续")

        self.assertIn("未覆盖子问题：方案 B 的限制", instruction)
        self.assertIn("必须明确披露未覆盖范围", prompt)
        self.assertIn("方案 B 的限制", prompt)


if __name__ == "__main__":
    unittest.main()
