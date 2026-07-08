import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.chat.request_context import ChatRequestContext

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeStructuredInvoker:
    def __init__(self, schema, handler):
        self.schema = schema
        self.handler = handler

    def invoke(self, messages):
        content = messages[0]["content"] if messages and isinstance(messages[0], dict) else str(messages)
        payload = self.handler(self.schema, content)
        return self.schema(**payload)


class FakeStructuredModel:
    def __init__(self, handler):
        self.handler = handler

    def with_structured_output(self, schema):
        return FakeStructuredInvoker(schema, self.handler)


def _dedupe_documents(docs):
    seen = set()
    out = []
    for doc in docs:
        key = doc.get("chunk_id") or doc.get("text")
        if key in seen:
            continue
        seen.add(key)
        out.append(doc)
    return out


def load_pipeline(
    *,
    retrieve_documents,
    step_back_expand=None,
    generate_hypothetical_document=None,
):
    fake_rag = types.ModuleType("backend.rag")
    fake_rag.__path__ = []

    fake_utils = types.ModuleType("backend.rag.utils")
    fake_utils.retrieve_documents = retrieve_documents
    fake_utils.step_back_expand = step_back_expand or (lambda query: {
        "step_back_question": "",
        "step_back_answer": "",
        "expanded_query": f"rewritten {query}",
    })
    fake_utils.generate_hypothetical_document = generate_hypothetical_document or (lambda query: f"hyde {query}")
    fake_utils.dedupe_documents = _dedupe_documents
    fake_utils.retrieval_trace_fields = lambda meta: dict(meta)
    fake_utils.merge_retrieval_trace = lambda acc, meta: {**acc, **meta}

    module_name = f"rag_pipeline_under_test_{id(retrieve_documents)}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / "backend" / "rag" / "pipeline.py",
    )
    module = importlib.util.module_from_spec(spec)

    with patch.dict(sys.modules, {"backend.rag": fake_rag, "backend.rag.utils": fake_utils}):
        spec.loader.exec_module(module)

    return module


def _doc(text, chunk_id="chunk-1", filename="doc.md"):
    return {
        "filename": filename,
        "page_number": 1,
        "text": text,
        "chunk_id": chunk_id,
    }


def _meta(count):
    return {
        "retrieval_mode": "hybrid",
        "retrieval_pipeline": "recall_merge_rerank",
        "candidate_k": count,
        "retrieval_top_k": 5,
        "recall_count": count,
        "retrieval_empty": count == 0,
    }


class RagShortCircuitTests(unittest.TestCase):
    def _ctx(self):
        return ChatRequestContext.for_sync(user_id="u", session_id="s")

    def test_simple_no_retrieval_short_circuits_without_rewrite(self):
        calls = {"retrieve": 0, "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"] += 1
            return {"docs": [], "meta": _meta(0)}

        def step_back(query):
            calls["step_back"] += 1
            return {"expanded_query": f"rewritten {query}"}

        pipeline = load_pipeline(retrieve_documents=retrieve, step_back_expand=step_back)
        pipeline._get_complexity_model = lambda: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda: FakeStructuredModel(lambda schema, prompt: {})

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("uncovered question", ctx)
        finally:
            ctx.close()

        self.assertEqual([], result.get("docs"))
        self.assertEqual("no_knowledge", result.get("retrieval_status"))
        self.assertEqual("no_knowledge", result.get("rag_trace", {}).get("retrieval_status"))
        self.assertEqual(1, calls["retrieve"])
        self.assertEqual(0, calls["step_back"])

    def test_strong_evidence_returns_after_initial_grade(self):
        calls = {"retrieve": 0, "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"] += 1
            return {"docs": [_doc("direct answer evidence")], "meta": _meta(1)}

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.93,
            }

        pipeline = load_pipeline(
            retrieve_documents=retrieve,
            step_back_expand=lambda query: calls.__setitem__("step_back", calls["step_back"] + 1) or {},
        )
        pipeline._get_complexity_model = lambda: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("covered question", ctx)
        finally:
            ctx.close()

        self.assertEqual(1, len(result.get("docs", [])))
        self.assertEqual("answerable", result.get("retrieval_status"))
        self.assertEqual(1, calls["retrieve"])
        self.assertEqual(0, calls["step_back"])

    def test_weak_evidence_rewrites_once_then_clarifies(self):
        calls = {"retrieve": [], "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            if query.startswith("rewritten"):
                return {"docs": [_doc("still partial evidence", "chunk-2")], "meta": _meta(1)}
            return {"docs": [_doc("weak evidence", "chunk-1")], "meta": _meta(1)}

        def grade(schema, prompt):
            return {
                "relevance": "weak",
                "answerability": "partial",
                "ambiguity": "none",
                "route": "rewrite",
                "confidence": 0.44,
            }

        def step_back(query):
            calls["step_back"] += 1
            return {
                "step_back_question": "general?",
                "step_back_answer": "general answer",
                "expanded_query": f"rewritten {query}",
            }

        pipeline = load_pipeline(retrieve_documents=retrieve, step_back_expand=step_back)
        pipeline._get_complexity_model = lambda: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda: FakeStructuredModel(grade)
        pipeline._get_router_model = lambda: None

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("weak question", ctx)
        finally:
            ctx.close()

        self.assertEqual(["weak question", "rewritten weak question"], calls["retrieve"])
        self.assertEqual(1, calls["step_back"])
        self.assertEqual("needs_clarification", result.get("retrieval_status"))
        self.assertEqual([], result.get("docs"))

    def test_missing_slot_and_scope_select_do_not_rewrite(self):
        cases = [
            ("missing_slot", "clarify", "needs_clarification"),
            ("multiple_candidates", "scope_select", "needs_scope_selection"),
        ]
        for ambiguity, route, status in cases:
            with self.subTest(ambiguity=ambiguity):
                calls = {"retrieve": 0, "step_back": 0}

                def retrieve(query, top_k=5):
                    calls["retrieve"] += 1
                    return {"docs": [_doc("related but ambiguous")], "meta": _meta(1)}

                def grade(schema, prompt):
                    return {
                        "relevance": "strong",
                        "answerability": "partial",
                        "ambiguity": ambiguity,
                        "route": route,
                        "confidence": 0.61,
                        "missing_slots": ["版本"] if ambiguity == "missing_slot" else [],
                        "hitl_prompt": "请补充版本" if ambiguity == "missing_slot" else "请选择方向",
                        "hitl_options": ["A", "B"] if ambiguity == "multiple_candidates" else [],
                    }

                pipeline = load_pipeline(
                    retrieve_documents=retrieve,
                    step_back_expand=lambda query: calls.__setitem__("step_back", calls["step_back"] + 1) or {},
                )
                pipeline._get_complexity_model = lambda: FakeStructuredModel(
                    lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
                )
                pipeline._get_grader_model = lambda: FakeStructuredModel(grade)

                ctx = self._ctx()
                try:
                    result = pipeline.run_rag_graph("ambiguous question", ctx)
                finally:
                    ctx.close()

                self.assertEqual(status, result.get("retrieval_status"))
                self.assertEqual([], result.get("docs"))
                self.assertEqual(1, calls["retrieve"])
                self.assertEqual(0, calls["step_back"])

    def test_hitl_result_includes_resume_state_with_candidate_docs(self):
        def retrieve(query, top_k=5):
            return {"docs": [_doc("丹瑾和丹恒都可能相关", "candidate")], "meta": _meta(1)}

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "partial",
                "ambiguity": "missing_slot",
                "route": "clarify",
                "confidence": 0.7,
                "missing_slots": ["角色名"],
                "hitl_prompt": "请补充角色名",
                "hitl_options": ["丹瑾", "丹恒"],
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("这个角色的属性是什么？", ctx)
        finally:
            ctx.close()

        resume_state = result.get("hitl_resume_state")
        self.assertIsInstance(resume_state, dict)
        self.assertEqual("这个角色的属性是什么？", resume_state.get("question"))
        self.assertEqual("needs_clarification", resume_state.get("retrieval_status"))
        self.assertEqual(1, len(resume_state.get("candidate_docs", [])))
        self.assertEqual(1, len(result.get("rag_trace", {}).get("hitl_candidate_chunks", [])))

    def test_resume_goes_directly_to_targeted_retrieval_after_hitl_answer(self):
        calls = {"retrieve": []}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            return {"docs": [_doc("丹瑾是湮灭属性", "retrieved")], "meta": _meta(1)}

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_grader_model = lambda: FakeStructuredModel(grade)
        resume_state = {
            "version": 1,
            "question": "这个角色的属性是什么？",
            "route": "clarify",
            "retrieval_status": "needs_clarification",
            "candidate_docs": [_doc("旧候选不应该被直接评分", "candidate")],
            "rag_trace": {
                "tool_used": True,
                "tool_name": "search_knowledge_base",
                "query": "这个角色的属性是什么？",
            },
        }

        ctx = self._ctx()
        try:
            result = pipeline.resume_rag_from_hitl(resume_state, "丹瑾", ctx)
        finally:
            ctx.close()

        self.assertEqual(["丹瑾：这个角色的属性是什么？"], calls["retrieve"])
        self.assertEqual("answerable", result.get("retrieval_status"))
        self.assertEqual(1, len(result.get("docs", [])))
        self.assertTrue(result.get("rag_trace", {}).get("hitl_resumed"))
        self.assertEqual("targeted_retrieval", result.get("rag_trace", {}).get("hitl_resume_strategy"))
        self.assertEqual("hitl_targeted_retrieval", result.get("rag_trace", {}).get("retrieval_stage"))

    def test_complex_sub_agents_keep_partial_docs_without_rewrite(self):
        calls = {"retrieve": [], "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            if query == "known sub":
                return {"docs": [_doc("partial sub evidence", "known")], "meta": _meta(1)}
            return {"docs": [], "meta": _meta(0)}

        def complexity(schema, prompt):
            if schema.__name__ == "ComplexityResult":
                return {"complexity": "complex", "reason": "unit"}
            return {"sub_questions": ["known sub", "unknown sub"]}

        def grade(schema, prompt):
            return {
                "relevance": "weak",
                "answerability": "partial",
                "ambiguity": "none",
                "route": "rewrite",
                "confidence": 0.5,
            }

        pipeline = load_pipeline(
            retrieve_documents=retrieve,
            step_back_expand=lambda query: calls.__setitem__("step_back", calls["step_back"] + 1) or {},
        )
        pipeline._get_complexity_model = lambda: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex question", ctx)
        finally:
            ctx.close()

        self.assertCountEqual(["known sub", "unknown sub"], calls["retrieve"])
        self.assertEqual(0, calls["step_back"])
        self.assertEqual(1, len(result.get("docs", [])))
        self.assertEqual("partial", result.get("retrieval_status"))

    def test_complex_all_no_knowledge_synthesizes_no_knowledge(self):
        calls = {"retrieve": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"] += 1
            return {"docs": [], "meta": _meta(0)}

        def complexity(schema, prompt):
            if schema.__name__ == "ComplexityResult":
                return {"complexity": "complex", "reason": "unit"}
            return {"sub_questions": ["missing one", "missing two"]}

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda: FakeStructuredModel(lambda schema, prompt: {})

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex uncovered", ctx)
        finally:
            ctx.close()

        self.assertEqual(2, calls["retrieve"])
        self.assertEqual([], result.get("docs"))
        self.assertEqual("no_knowledge", result.get("retrieval_status"))

    def test_complex_preserves_sub_agent_hitl_when_no_docs_can_be_synthesized(self):
        def retrieve(query, top_k=5):
            return {"docs": [_doc("ambiguous related evidence", query)], "meta": _meta(1)}

        def complexity(schema, prompt):
            if schema.__name__ == "ComplexityResult":
                return {"complexity": "complex", "reason": "unit"}
            return {"sub_questions": ["feature of it", "genesis of it"]}

        def grade(schema, prompt):
            return {
                "relevance": "weak",
                "answerability": "none",
                "ambiguity": "missing_slot",
                "route": "clarify",
                "confidence": 0.4,
                "missing_slots": ["指代对象"],
                "hitl_prompt": "请说明你说的它具体指什么。",
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("它的主要特征和成因是什么？", ctx)
        finally:
            ctx.close()

        self.assertEqual([], result.get("docs"))
        self.assertEqual("needs_clarification", result.get("retrieval_status"))
        self.assertEqual("clarify", result.get("route"))
        self.assertIn("具体指什么", result.get("hitl_prompt", ""))


if __name__ == "__main__":
    unittest.main()
