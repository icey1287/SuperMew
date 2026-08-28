import importlib.util
import hashlib
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.core.errors import AppError, ErrorCode
from backend.runs.request_context import RunRequestContext
from backend.providers import ProviderCode, ProviderError, ProviderOperation

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeStructuredInvoker:
    def __init__(self, schema, handler):
        self.schema = schema
        self.handler = handler

    def invoke(self, messages):
        content = (
            messages[0]["content"]
            if messages and isinstance(messages[0], dict)
            else str(messages)
        )
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
    rewrite_query_once=None,
    resolve_retrieval_snapshot=None,
):
    # Keep LangGraph's contextvars and exception classes stable after patch.dict
    # restores sys.modules. Otherwise interrupt() can observe a second module copy.
    for dependency_name in (
        "langgraph.checkpoint.memory",
        "langgraph.config",
        "langgraph.errors",
        "langgraph.graph",
        "langgraph.types",
    ):
        importlib.import_module(dependency_name)

    fake_rag = types.ModuleType("backend.rag")
    fake_rag.__path__ = []

    fake_utils = types.ModuleType("backend.rag.utils")
    fake_utils.RETRIEVAL_TOP_K = 5
    retrieve_parameters = inspect.signature(retrieve_documents).parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in retrieve_parameters.values()
    )

    def fake_retrieve_documents(query, top_k=5, **kwargs):
        forwarded = (
            kwargs
            if accepts_kwargs
            else {
                key: value
                for key, value in kwargs.items()
                if key in retrieve_parameters
            }
        )
        return retrieve_documents(query, top_k=top_k, **forwarded)

    fake_utils.retrieve_documents = fake_retrieve_documents
    fake_utils.resolve_retrieval_snapshot = resolve_retrieval_snapshot or (
        lambda **_kwargs: object()
    )
    rewrite_impl = rewrite_query_once or (
        lambda query: {
            "rewrite_method": "step_back",
            "step_back_question": "broader question",
            "hyde_document": "",
            "rewritten_query": f"rewritten {query}",
        }
    )
    fake_utils.rewrite_query_once = lambda query, **kwargs: rewrite_impl(query)
    fake_utils.dedupe_documents = _dedupe_documents
    fake_utils.retrieval_trace_fields = lambda meta: dict(meta)

    module_name = f"rag_pipeline_under_test_{id(retrieve_documents)}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / "backend" / "rag" / "pipeline.py",
    )
    module = importlib.util.module_from_spec(spec)
    runtime_spec = importlib.util.spec_from_file_location(
        "backend.rag.runtime_context",
        REPO_ROOT / "backend" / "rag" / "runtime_context.py",
    )
    runtime_module = importlib.util.module_from_spec(runtime_spec)
    outcomes_spec = importlib.util.spec_from_file_location(
        "backend.rag.outcomes",
        REPO_ROOT / "backend" / "rag" / "outcomes.py",
    )
    outcomes_module = importlib.util.module_from_spec(outcomes_spec)
    evidence_spec = importlib.util.spec_from_file_location(
        "backend.rag.evidence",
        REPO_ROOT / "backend" / "rag" / "evidence.py",
    )
    evidence_module = importlib.util.module_from_spec(evidence_spec)

    with patch.dict(
        sys.modules,
        {
            "backend.rag": fake_rag,
            "backend.rag.utils": fake_utils,
            "backend.rag.runtime_context": runtime_module,
            "backend.rag.outcomes": outcomes_module,
            "backend.rag.evidence": evidence_module,
        },
    ):
        runtime_spec.loader.exec_module(runtime_module)
        outcomes_spec.loader.exec_module(outcomes_module)
        evidence_spec.loader.exec_module(evidence_module)
        spec.loader.exec_module(module)

    return module


def _doc(text, chunk_id="chunk-1", filename="doc.md"):
    return {
        "filename": filename,
        "page_number": 1,
        "text": text,
        "chunk_id": chunk_id,
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "section_id": "section-1",
        "index_version": "catalog-v1",
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
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
        return RunRequestContext.for_sync(
            user_id="u",
            thread_id="s",
            tenant_id="tenant-a",
        )

    def test_two_tenants_keep_initial_retrieval_scoped_to_their_context(self):
        seen_tenants = []

        def retrieve(query, top_k=5, *, tenant_id):
            del query, top_k
            seen_tenants.append(tenant_id)
            return {
                "docs": [_doc(f"evidence for {tenant_id}", chunk_id=tenant_id)],
                "meta": _meta(1),
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 1.0,
            }
        )

        results = {}
        for tenant_id in ("tenant-a", "tenant-b"):
            ctx = RunRequestContext.for_sync(
                user_id="u",
                thread_id=f"thread-{tenant_id}",
                tenant_id=tenant_id,
            )
            try:
                results[tenant_id] = pipeline.run_rag_graph("scoped question", ctx)
            finally:
                ctx.close()

        self.assertEqual(["tenant-a", "tenant-b"], seen_tenants)
        self.assertEqual(
            "evidence for tenant-a",
            results["tenant-a"]["docs"][0]["text"],
        )
        self.assertEqual(
            "evidence for tenant-b",
            results["tenant-b"]["docs"][0]["text"],
        )

    def test_rag_fails_closed_when_request_context_has_no_tenant(self):
        calls = []
        pipeline = load_pipeline(
            retrieve_documents=lambda query, top_k=5, **kwargs: calls.append(
                (query, top_k, kwargs)
            )
        )
        ctx = RunRequestContext.for_sync(user_id="u", thread_id="tenantless")
        try:
            with self.assertRaisesRegex(ValueError, "tenant_id"):
                pipeline.run_rag_graph("scoped question", ctx)
        finally:
            ctx.close()

        self.assertEqual([], calls)

    def test_hitl_resume_rejects_a_context_from_another_tenant(self):
        seen_tenants = []
        grade_calls = 0

        def retrieve(query, top_k=5, *, tenant_id):
            del top_k
            seen_tenants.append(tenant_id)
            return {
                "docs": [_doc(f"{tenant_id}: {query}", chunk_id=query)],
                "meta": _meta(1),
            }

        def grade(schema, prompt):
            del schema, prompt
            nonlocal grade_calls
            grade_calls += 1
            if grade_calls == 1:
                return {
                    "relevance": "strong",
                    "answerability": "partial",
                    "ambiguity": "missing_slot",
                    "route": "clarify",
                    "confidence": 0.8,
                    "missing_slots": ["角色名"],
                    "hitl_prompt": "请补充角色名",
                }
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 1.0,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)
        ctx_a = RunRequestContext.for_sync(
            user_id="u",
            thread_id="thread-a",
            tenant_id="tenant-a",
        )
        ctx_b = RunRequestContext.for_sync(
            user_id="u",
            thread_id="thread-b",
            tenant_id="tenant-b",
        )
        try:
            paused = pipeline.run_rag_graph("这个角色是什么属性？", ctx_a)
            with self.assertRaises(AppError) as raised:
                pipeline.resume_rag_from_hitl(
                    paused["hitl_resume_state"],
                    "丹瑾",
                    ctx_b,
                )
            resumed = pipeline.resume_rag_from_hitl(
                paused["hitl_resume_state"],
                "丹瑾",
                ctx_a,
            )
        finally:
            ctx_a.close()
            ctx_b.close()

        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, raised.exception.code)
        self.assertEqual("answerable", resumed["retrieval_status"])
        self.assertEqual(["tenant-a", "tenant-a"], seen_tenants)

    def test_simple_no_retrieval_short_circuits_without_rewrite(self):
        calls = {"retrieve": 0, "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"] += 1
            return {"docs": [], "meta": _meta(0)}

        def step_back(query):
            calls["step_back"] += 1
            return {
                "rewrite_method": "step_back",
                "step_back_question": "broader question",
                "hyde_document": "",
                "rewritten_query": f"rewritten {query}",
            }

        pipeline = load_pipeline(
            retrieve_documents=retrieve, rewrite_query_once=step_back
        )
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {}
        )

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("uncovered question", ctx)
        finally:
            ctx.close()

        self.assertEqual([], result.get("docs"))
        self.assertEqual("no_knowledge", result.get("retrieval_status"))
        self.assertEqual("NO_KNOWLEDGE", result.get("retrieval_outcome"))
        self.assertFalse(result.get("rag_trace", {}).get("coverage_gap_questions"))
        self.assertEqual(
            "no_knowledge", result.get("rag_trace", {}).get("retrieval_status")
        )
        self.assertEqual(1, calls["retrieve"])
        self.assertEqual(0, calls["step_back"])

    def test_obvious_simple_question_skips_complexity_model(self):
        def retrieve(query, top_k=5):
            return {"docs": [_doc("丹瑾是湮灭属性")], "meta": _meta(1)}

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.95,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        complexity_model = Mock(
            return_value=FakeStructuredModel(
                lambda schema, prompt: {"complexity": "simple", "reason": "model"}
            )
        )
        pipeline._get_complexity_model = complexity_model
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("丹瑾是什么属性？", ctx)
        finally:
            ctx.close()

        complexity_model.assert_not_called()
        self.assertEqual("simple", result.get("complexity"))
        self.assertIn("fast_path", result.get("complexity_reason", ""))

    def test_entity_version_attribute_query_skips_complexity_model(self):
        def retrieve(query, top_k=5):
            return {"docs": [_doc("Orion V2 外壳规格")], "meta": _meta(1)}

        pipeline = load_pipeline(retrieve_documents=retrieve)
        complexity_model = Mock(
            return_value=FakeStructuredModel(
                lambda schema, prompt: {"complexity": "simple", "reason": "model"}
            )
        )
        pipeline._get_complexity_model = complexity_model
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.95,
            }
        )

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("Orion V2 外壳标准色号", ctx)
        finally:
            ctx.close()

        complexity_model.assert_not_called()
        self.assertEqual("simple", result.get("complexity"))
        self.assertIn("fast_path", result.get("complexity_reason", ""))

    def test_multi_dimension_keyword_query_still_uses_complexity_model(self):
        def retrieve(query, top_k=5):
            return {"docs": [_doc("comparison evidence")], "meta": _meta(1)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "multiple entities and dimensions",
                "sub_questions": ["丹瑾的属性与武器", "卡卡罗的属性与武器"],
            }

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        complexity_model_calls = {"count": 0}

        def get_complexity_model(*_):
            complexity_model_calls["count"] += 1
            return FakeStructuredModel(complexity)

        pipeline._get_complexity_model = get_complexity_model
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("丹瑾 卡卡罗 属性 武器类型 战斗定位", ctx)
        finally:
            ctx.close()

        self.assertGreaterEqual(complexity_model_calls["count"], 1)
        self.assertEqual("complex", result.get("complexity"))
        self.assertEqual(2, result.get("rag_trace", {}).get("sub_agent_count"))

    def test_multi_fact_interrogatives_do_not_take_the_simple_fast_path(self):
        def retrieve(query, top_k=5):
            return {"docs": [_doc("cross-document evidence")], "meta": _meta(1)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "multiple requested facts",
                "sub_questions": ["北仓部署数量", "北仓支持套餐与响应时间"],
            }

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        complexity_model_calls = {"count": 0}

        def get_complexity_model(*_):
            complexity_model_calls["count"] += 1
            return FakeStructuredModel(complexity)

        pipeline._get_complexity_model = get_complexity_model
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph(
                "北仓部署了多少台 Orion，采用哪个支持套餐，该套餐的响应时间是多少？",
                ctx,
            )
        finally:
            ctx.close()

        self.assertGreaterEqual(complexity_model_calls["count"], 1)
        self.assertEqual("complex", result.get("complexity"))
        self.assertEqual(2, result.get("rag_trace", {}).get("sub_agent_count"))

    def test_complex_subquestions_share_one_catalog_snapshot_per_request(self):
        catalog_calls = []
        retrieval_snapshots = []
        snapshot = types.SimpleNamespace(tenant_id="tenant-a")

        def resolve_snapshot(**kwargs):
            catalog_calls.append(kwargs)
            return snapshot

        def retrieve(
            query,
            top_k=5,
            *,
            tenant_id,
            retrieval_snapshot,
        ):
            del top_k
            self.assertEqual("tenant-a", tenant_id)
            retrieval_snapshots.append(retrieval_snapshot)
            return {"docs": [_doc(f"evidence for {query}", query)], "meta": _meta(1)}

        pipeline = load_pipeline(
            retrieve_documents=retrieve,
            resolve_retrieval_snapshot=resolve_snapshot,
        )
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {
                "complexity": "complex",
                "reason": "comparison",
                "sub_questions": ["question-a", "question-b", "question-c"],
            }
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }
        )

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("compare three dimensions", ctx)
        finally:
            ctx.close()

        self.assertEqual(3, result.get("rag_trace", {}).get("sub_agent_count"))
        self.assertEqual(
            [{"tenant_id": "tenant-a", "deadline": None, "cancellation": None}],
            catalog_calls,
        )
        self.assertEqual([snapshot, snapshot, snapshot], retrieval_snapshots)

    def test_complexity_plan_includes_child_queries(self):
        model_schemas = []

        def retrieve(query, top_k=5):
            return {"docs": [_doc(f"evidence for {query}", query)], "meta": _meta(1)}

        def plan(schema, prompt):
            model_schemas.append(schema.__name__)
            return {
                "complexity": "complex",
                "reason": "comparison",
                "sub_questions": ["丹瑾的定位", "卡卡罗的定位"],
            }

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(plan)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("比较丹瑾与卡卡罗的战斗定位", ctx)
        finally:
            ctx.close()

        self.assertEqual(["ComplexityResult"], model_schemas)
        self.assertEqual(2, result.get("rag_trace", {}).get("sub_agent_count"))

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
            rewrite_query_once=lambda query: (
                calls.__setitem__("step_back", calls["step_back"] + 1) or {}
            ),
        )
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("covered question", ctx)
        finally:
            ctx.close()

        self.assertEqual(1, len(result.get("docs", [])))
        self.assertEqual("answerable", result.get("retrieval_status"))
        self.assertEqual(1, calls["retrieve"])
        self.assertEqual(0, calls["step_back"])

    def test_grader_uses_compact_evidence_without_shrinking_answer_context(self):
        captured_prompts = []
        documents = [
            _doc(
                f"evidence-{index}-" + (chr(97 + index) * 1400),
                chunk_id=f"chunk-{index}",
            )
            for index in range(8)
        ]

        def retrieve(query, top_k=5):
            return {"docs": documents, "meta": _meta(len(documents))}

        def grade(schema, prompt):
            captured_prompts.append(prompt)
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.93,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline.grader_evidence_character_budget = lambda: 1600
        pipeline.grader_max_document_character_budget = lambda: 500
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("covered compact grader question", ctx)
        finally:
            ctx.close()

        trace = result.get("rag_trace", {})
        self.assertGreater(len(result.get("context", "")), 8000)
        self.assertLessEqual(trace["grader_evidence_characters"], 1600)
        self.assertGreater(trace["grader_evidence_omitted_count"], 0)
        self.assertGreater(trace["grader_evidence_truncated_count"], 0)
        self.assertLess(len(captured_prompts[0]), 3500)
        self.assertIn("evidence-0", captured_prompts[0])
        self.assertNotIn("evidence-7", captured_prompts[0])

    def test_weak_evidence_rewrites_once_then_clarifies(self):
        calls = {"retrieve": [], "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            if query.startswith("rewritten"):
                return {
                    "docs": [_doc("still partial evidence", "chunk-2")],
                    "meta": _meta(1),
                }
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
                "rewrite_method": "step_back",
                "step_back_question": "general?",
                "hyde_document": "",
                "rewritten_query": f"rewritten {query}",
            }

        pipeline = load_pipeline(
            retrieve_documents=retrieve, rewrite_query_once=step_back
        )
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("weak question", ctx)
        finally:
            ctx.close()

        self.assertEqual(
            ["weak question", "rewritten weak question"], calls["retrieve"]
        )
        self.assertEqual(1, calls["step_back"])
        self.assertEqual("needs_clarification", result.get("retrieval_status"))
        self.assertEqual([], result.get("docs"))

    def test_hyde_rewrite_runs_only_selected_retrieval(self):
        calls = {"retrieve": [], "rewrite": 0, "grade": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            return {"docs": [_doc(f"evidence for {query}")], "meta": _meta(1)}

        def grade(schema, prompt):
            calls["grade"] += 1
            if calls["grade"] == 1:
                return {
                    "relevance": "weak",
                    "answerability": "partial",
                    "ambiguity": "none",
                    "route": "rewrite",
                    "confidence": 0.5,
                }
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        def rewrite(query):
            calls["rewrite"] += 1
            return {
                "rewrite_method": "hyde",
                "step_back_question": "",
                "hyde_document": "一段用于召回真实证据的假设性答案",
                "rewritten_query": "HyDE rewritten query",
            }

        pipeline = load_pipeline(
            retrieve_documents=retrieve, rewrite_query_once=rewrite
        )
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("模糊的概念问题", ctx)
        finally:
            ctx.close()

        self.assertEqual(["模糊的概念问题", "HyDE rewritten query"], calls["retrieve"])
        self.assertEqual(1, calls["rewrite"])
        self.assertEqual(2, calls["grade"])
        self.assertEqual("hyde", result.get("rag_trace", {}).get("rewrite_method"))
        self.assertIn(
            "假设性答案", result.get("rag_trace", {}).get("hyde_document", "")
        )
        self.assertNotIn("step_back_question", result.get("rag_trace", {}))

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
                        "missing_slots": ["版本"]
                        if ambiguity == "missing_slot"
                        else [],
                        "hitl_prompt": "请补充版本"
                        if ambiguity == "missing_slot"
                        else "请选择方向",
                        "hitl_options": ["A", "B"]
                        if ambiguity == "multiple_candidates"
                        else [],
                    }

                pipeline = load_pipeline(
                    retrieve_documents=retrieve,
                    rewrite_query_once=lambda query: (
                        calls.__setitem__("step_back", calls["step_back"] + 1) or {}
                    ),
                )
                pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
                    lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
                )
                pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

                ctx = self._ctx()
                try:
                    result = pipeline.run_rag_graph("ambiguous question", ctx)
                finally:
                    ctx.close()

                self.assertEqual(status, result.get("retrieval_status"))
                self.assertEqual([], result.get("docs"))
                self.assertEqual(1, calls["retrieve"])
                self.assertEqual(0, calls["step_back"])

    def test_hitl_result_includes_only_current_resume_state(self):
        def retrieve(query, top_k=5):
            return {
                "docs": [_doc("丹瑾和丹恒都可能相关", "candidate")],
                "meta": _meta(1),
            }

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
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("这个角色的属性是什么？", ctx)
        finally:
            ctx.close()

        resume_state = result.get("hitl_resume_state")
        self.assertIsInstance(resume_state, dict)
        self.assertEqual("这个角色的属性是什么？", resume_state.get("question"))
        self.assertEqual("clarify", result.get("route"))
        self.assertEqual("needs_clarification", resume_state.get("retrieval_status"))
        self.assertEqual(
            {
                "question",
                "route",
                "retrieval_status",
                "rewrite_count",
                "complexity",
                "complexity_reason",
                "sub_questions",
                "checkpoint_thread_id",
                "checkpoint_id",
                "interrupt_id",
            },
            set(resume_state),
        )
        self.assertTrue(resume_state["checkpoint_thread_id"].startswith("rag_"))
        self.assertTrue(resume_state["checkpoint_id"])
        self.assertTrue(resume_state["interrupt_id"])

    def test_explicit_version_question_with_options_uses_scope_select(self):
        def retrieve(query, top_k=5):
            return {
                "docs": [_doc("Orion V2 和 V2.1 都有版本记录", "candidate")],
                "meta": _meta(1),
            }

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "partial",
                "ambiguity": "missing_slot",
                "route": "clarify",
                "confidence": 0.8,
                "missing_slots": ["版本"],
                "hitl_prompt": "请选择版本",
                "hitl_options": ["V2", "V2.1"],
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {"complexity": "simple", "reason": "unit"}
        )
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph(
                "Orion 的额定载荷是多少？请按我正在使用的版本回答。",
                ctx,
            )
        finally:
            ctx.close()

        self.assertEqual("scope_select", result.get("route"))
        self.assertEqual("needs_scope_selection", result.get("retrieval_status"))

    def test_complex_sub_agents_keep_partial_docs_without_rewrite(self):
        calls = {"retrieve": [], "step_back": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            if query == "known sub":
                return {
                    "docs": [_doc("partial sub evidence", "known")],
                    "meta": _meta(1),
                }
            return {"docs": [], "meta": _meta(0)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["known sub", "unknown sub"],
            }

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
            rewrite_query_once=lambda query: (
                calls.__setitem__("step_back", calls["step_back"] + 1) or {}
            ),
        )
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex question", ctx)
        finally:
            ctx.close()

        self.assertCountEqual(["known sub", "unknown sub"], calls["retrieve"])
        self.assertEqual(0, calls["step_back"])
        self.assertEqual(1, len(result.get("docs", [])))
        self.assertEqual("partial", result.get("retrieval_status"))
        self.assertEqual("INSUFFICIENT_EVIDENCE", result.get("retrieval_outcome"))

    def test_complex_sufficient_branch_plus_healthy_empty_branch_is_partial(self):
        def retrieve(query, top_k=5):
            if query == "known sub":
                return {
                    "docs": [_doc("complete known evidence", "known")],
                    "meta": _meta(1),
                }
            return {"docs": [], "meta": _meta(0)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["known sub", "unknown sub"],
            }

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex mixed coverage", ctx)
        finally:
            ctx.close()

        self.assertEqual(1, len(result.get("docs", [])))
        self.assertEqual("partial", result.get("retrieval_status"))
        self.assertEqual("INSUFFICIENT_EVIDENCE", result.get("retrieval_outcome"))
        self.assertEqual(
            ["unknown sub"],
            result.get("rag_trace", {}).get("coverage_gap_questions"),
        )

    def test_complex_all_no_knowledge_synthesizes_no_knowledge(self):
        calls = {"retrieve": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"] += 1
            return {"docs": [], "meta": _meta(0)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["missing one", "missing two"],
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {}
        )

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex uncovered", ctx)
        finally:
            ctx.close()

        self.assertEqual(2, calls["retrieve"])
        self.assertEqual([], result.get("docs"))
        self.assertEqual("no_knowledge", result.get("retrieval_status"))
        self.assertEqual("NO_KNOWLEDGE", result.get("retrieval_outcome"))

    def test_complex_provider_failure_with_docs_marks_coverage_gap(self):
        def retrieve(query, top_k=5):
            if query == "failed sub":
                raise ProviderError.from_code(
                    ProviderCode.VECTOR_STORE_UNAVAILABLE,
                    provider="milvus",
                    operation=ProviderOperation.VECTOR_SEARCH,
                )
            return {"docs": [_doc("known evidence", query)], "meta": _meta(1)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["known sub", "failed sub"],
            }

        def grade(schema, prompt):
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.9,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex partial failure", ctx)
        finally:
            ctx.close()

        self.assertEqual(1, len(result.get("docs", [])))
        self.assertEqual("partial", result.get("retrieval_status"))
        self.assertEqual(
            ["VECTOR_STORE_UNAVAILABLE"],
            result.get("rag_trace", {}).get("coverage_gap_codes"),
        )

    def test_complex_healthy_empty_plus_provider_failure_is_insufficient(self):
        def retrieve(query, top_k=5):
            if query == "failed sub":
                raise ProviderError.from_code(
                    ProviderCode.VECTOR_STORE_UNAVAILABLE,
                    provider="milvus",
                    operation=ProviderOperation.VECTOR_SEARCH,
                )
            return {"docs": [], "meta": _meta(0)}

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["healthy empty", "failed sub"],
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {}
        )

        ctx = self._ctx()
        try:
            result = pipeline.run_rag_graph("complex mixed outage", ctx)
        finally:
            ctx.close()

        self.assertEqual([], result.get("docs"))
        self.assertEqual("insufficient_evidence", result.get("retrieval_status"))
        self.assertEqual("INSUFFICIENT_EVIDENCE", result.get("retrieval_outcome"))
        self.assertEqual(
            ["VECTOR_STORE_UNAVAILABLE"],
            result.get("rag_trace", {}).get("coverage_gap_codes"),
        )

    def test_complex_all_provider_failures_raise_typed_error(self):
        def retrieve(query, top_k=5):
            raise ProviderError.from_code(
                ProviderCode.EMBEDDING_UNAVAILABLE,
                provider="embedding-model",
                operation=ProviderOperation.EMBEDDING,
            )

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["failed one", "failed two"],
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(
            lambda schema, prompt: {}
        )

        ctx = self._ctx()
        try:
            with self.assertRaises(ProviderError) as raised:
                pipeline.run_rag_graph("complex provider outage", ctx)
        finally:
            ctx.close()

        self.assertEqual(ProviderCode.EMBEDDING_UNAVAILABLE, raised.exception.code)

    def test_complex_preserves_sub_agent_hitl_when_no_docs_can_be_synthesized(self):
        def retrieve(query, top_k=5):
            return {
                "docs": [_doc("ambiguous related evidence", query)],
                "meta": _meta(1),
            }

        def complexity(schema, prompt):
            return {
                "complexity": "complex",
                "reason": "unit",
                "sub_questions": ["feature of it", "genesis of it"],
            }

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
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)

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
