from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

import backend.evaluation.runtime as runtime_module
from backend.evaluation import (
    RagEvalCase,
    RagExpectedBehavior,
    RagGoldDocument,
    RagOutcome,
    RagProviderErrorStage,
    RagRoute,
)
from backend.evaluation.runtime import RagEvaluationRuntime
from backend.providers import (
    ProviderCode,
    ProviderError,
    ProviderExecutor,
    ProviderOperation,
)
from tests.support import TEST_MODEL_SNAPSHOT


class _AnswerModel:
    def invoke(self, _messages):
        return AIMessage(content="证据说明丹瑾属于湮灭属性。[1]")


class _EvaluatorModel:
    def __init__(self):
        self.schema = None

    def with_structured_output(self, schema):
        self.schema = schema
        return self

    def invoke(self, _messages):
        return self.schema(
            answer_correctness=0.95,
            groundedness=0.9,
            answer_relevance=1,
            completeness=0.85,
            context_relevance=0.9,
            unsupported_claim_rate=0.05,
            conflict_disclosure_rate=1,
            reason="答案与检索证据一致",
        )


class _Models:
    def __init__(self):
        self.answer = _AnswerModel()
        self.evaluator = _EvaluatorModel()
        self.calls = []

    def get(self, role, *, snapshot):
        self.calls.append((role.value, snapshot.catalog_hash))
        return self.evaluator if role.value == "evaluator" else self.answer

    def describe(self, role, *, snapshot):
        assert snapshot.catalog_hash == TEST_MODEL_SNAPSHOT.catalog_hash
        return SimpleNamespace(name=f"{role.value}-model", timeout_seconds=15)


def test_runtime_generates_answer_and_structured_judge_without_persisting_evidence_text():
    case = RagEvalCase(
        id="answer-case",
        question="丹瑾是什么属性？",
        expected=RagExpectedBehavior(
            route=RagRoute.ANSWER,
            outcome=RagOutcome.ANSWERABLE,
        ),
        gold_documents=(RagGoldDocument(canonical_name="manual.md"),),
        reference_answer="丹瑾是湮灭属性角色。",
        required_claims=("丹瑾属于湮灭属性",),
    )
    document = {
        "filename": "manual.md",
        "page_number": 1,
        "text": "丹瑾是湮灭属性角色。",
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "index_version": "index-1",
        "content_hash": "a" * 64,
    }
    result = {
        "route": "answer",
        "retrieval_status": "answerable",
        "retrieval_outcome": "ANSWERABLE",
        "complexity": "simple",
        "docs": [document],
        "rag_trace": {
            "route": "answer",
            "retrieval_status": "answerable",
            "retrieval_outcome": "ANSWERABLE",
            "complexity": "simple",
            "retrieved_chunks": [document],
        },
    }
    models = _Models()
    runtime = RagEvaluationRuntime(
        models=models,
        executor=ProviderExecutor(sleeper=lambda _: None),
    )

    with patch.object(runtime_module, "run_rag_graph", return_value=result):
        execution = runtime.execute_case(
            job_id="rag_eval_1",
            case=case,
            model_snapshot=TEST_MODEL_SNAPSHOT,
            timeout_seconds=30,
            cancellation=lambda: False,
        )

    assert execution.generated_answer.endswith("[1]")
    assert execution.observation.judge is not None
    assert execution.observation.judge.answer_correctness == 0.95
    assert execution.judge_reason == "答案与检索证据一致"
    assert execution.retrieved_identities[0]["chunk_id"] == "chunk-1"
    assert "text" not in execution.retrieved_identities[0]
    assert [role for role, _ in models.calls] == ["answer", "evaluator"]


def test_retrieval_provider_failure_short_circuits_answer_and_judge():
    case = RagEvalCase(
        id="retrieval-failure",
        question="丹瑾是什么属性？",
        expected=RagExpectedBehavior(),
    )
    models = _Models()
    runtime = RagEvaluationRuntime(
        models=models,
        executor=ProviderExecutor(sleeper=lambda _: None),
    )
    error = ProviderError.from_code(
        ProviderCode.PROVIDER_TIMEOUT,
        provider="milvus",
        operation=ProviderOperation.VECTOR_SEARCH,
    )

    with patch.object(runtime_module, "run_rag_graph", side_effect=error):
        execution = runtime.execute_case(
            job_id="rag_eval_1",
            case=case,
            model_snapshot=TEST_MODEL_SNAPSHOT,
            timeout_seconds=30,
            cancellation=lambda: False,
        )

    assert execution.generated_answer == ""
    assert execution.judge is None
    assert execution.observation.provider_error_code == "PROVIDER_TIMEOUT"
    assert execution.observation.provider_error_stage is RagProviderErrorStage.RETRIEVAL
    assert models.calls == []


def test_partial_retrieval_provider_failure_with_docs_still_short_circuits():
    case = RagEvalCase(
        id="partial-retrieval-failure",
        question="比较两个维护流程",
        expected=RagExpectedBehavior(),
    )
    document = {
        "filename": "manual.md",
        "text": "只完成了其中一个分支的检索。",
        "chunk_id": "chunk-1",
    }
    result = {
        "route": "answer",
        "retrieval_status": "partial",
        "retrieval_outcome": "ANSWERABLE",
        "docs": [document],
        "rag_trace": {
            "route": "answer",
            "retrieved_chunks": [document],
            "sub_traces": [{"provider_error_code": "VECTOR_STORE_UNAVAILABLE"}],
        },
    }
    models = _Models()
    runtime = RagEvaluationRuntime(
        models=models,
        executor=ProviderExecutor(sleeper=lambda _: None),
    )

    with patch.object(runtime_module, "run_rag_graph", return_value=result):
        execution = runtime.execute_case(
            job_id="rag_eval_1",
            case=case,
            model_snapshot=TEST_MODEL_SNAPSHOT,
            timeout_seconds=30,
            cancellation=lambda: False,
        )

    assert execution.generated_answer == ""
    assert execution.judge is None
    assert execution.observation.provider_error_code == "VECTOR_STORE_UNAVAILABLE"
    assert execution.observation.provider_error_stage is RagProviderErrorStage.RETRIEVAL
    assert execution.retrieved_identities[0]["chunk_id"] == "chunk-1"
    assert models.calls == []


def test_generation_provider_failure_short_circuits_judge():
    class FailingAnswerModel:
        def invoke(self, _messages):
            raise ProviderError.from_code(
                ProviderCode.MODEL_TIMEOUT,
                provider="answer-model",
                operation=ProviderOperation.MODEL,
            )

    case = RagEvalCase(
        id="generation-failure",
        question="丹瑾是什么属性？",
        expected=RagExpectedBehavior(),
    )
    document = {
        "filename": "manual.md",
        "text": "丹瑾是湮灭属性角色。",
        "chunk_id": "chunk-1",
    }
    result = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "docs": [document],
        "rag_trace": {"route": "answer", "retrieved_chunks": [document]},
    }
    models = _Models()
    models.answer = FailingAnswerModel()
    runtime = RagEvaluationRuntime(
        models=models,
        executor=ProviderExecutor(sleeper=lambda _: None),
    )

    with patch.object(runtime_module, "run_rag_graph", return_value=result):
        execution = runtime.execute_case(
            job_id="rag_eval_1",
            case=case,
            model_snapshot=TEST_MODEL_SNAPSHOT,
            timeout_seconds=30,
            cancellation=lambda: False,
        )

    assert execution.generated_answer == ""
    assert execution.judge is None
    assert execution.observation.provider_error_code == "MODEL_TIMEOUT"
    assert (
        execution.observation.provider_error_stage is RagProviderErrorStage.GENERATION
    )
    assert [role for role, _ in models.calls] == ["answer"]


def test_judge_provider_failure_preserves_generated_answer_and_records_stage():
    class FailingEvaluatorModel(_EvaluatorModel):
        def invoke(self, _messages):
            raise ProviderError.from_code(
                ProviderCode.MODEL_UNAVAILABLE,
                provider="judge-model",
                operation=ProviderOperation.MODEL,
            )

    case = RagEvalCase(
        id="judge-failure",
        question="丹瑾是什么属性？",
        expected=RagExpectedBehavior(),
    )
    document = {
        "filename": "manual.md",
        "text": "丹瑾是湮灭属性角色。",
        "chunk_id": "chunk-1",
    }
    result = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "docs": [document],
        "rag_trace": {"route": "answer", "retrieved_chunks": [document]},
    }
    models = _Models()
    models.evaluator = FailingEvaluatorModel()
    runtime = RagEvaluationRuntime(
        models=models,
        executor=ProviderExecutor(sleeper=lambda _: None),
    )

    with patch.object(runtime_module, "run_rag_graph", return_value=result):
        execution = runtime.execute_case(
            job_id="rag_eval_1",
            case=case,
            model_snapshot=TEST_MODEL_SNAPSHOT,
            timeout_seconds=30,
            cancellation=lambda: False,
        )

    assert execution.generated_answer.endswith("[1]")
    assert execution.judge is None
    assert execution.observation.provider_error_code == "MODEL_UNAVAILABLE"
    assert execution.observation.provider_error_stage is RagProviderErrorStage.JUDGE
    assert [role for role, _ in models.calls] == ["answer", "evaluator"]


def test_hitl_answer_is_included_in_generation_and_judge_question():
    captured: dict[str, str] = {}

    class CapturingAnswerModel:
        def invoke(self, messages):
            captured["answer"] = messages[0]["content"]
            return AIMessage(content="Orion V2.1 的额定载荷为 25 千克。[1]")

    class CapturingEvaluatorModel(_EvaluatorModel):
        def invoke(self, messages):
            captured["judge"] = messages[0]["content"]
            return super().invoke(messages)

    case = RagEvalCase(
        id="scope-select",
        question="Orion 的额定载荷是多少？",
        expected=RagExpectedBehavior(
            route="scope_select",
            hitl="scope_select",
            acceptable_abstention=True,
            hitl_resolution_success=True,
            hitl_final_outcome="ANSWERABLE",
        ),
        hitl_answers=("V2.1",),
    )
    initial = {
        "route": "scope_select",
        "retrieval_status": "needs_scope_selection",
        "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
        "hitl_resume_state": {"checkpoint_thread_id": "thread"},
        "rag_trace": {
            "route": "scope_select",
            "retrieval_status": "needs_scope_selection",
        },
    }
    document = {
        "filename": "orion-versions.html",
        "text": "Orion V2.1 的额定载荷为 25 千克。",
        "chunk_id": "chunk-v21",
    }
    final = {
        "route": "answer",
        "retrieval_status": "answerable",
        "retrieval_outcome": "ANSWERABLE",
        "docs": [document],
        "rag_trace": {"route": "answer", "retrieved_chunks": [document]},
    }
    models = _Models()
    models.answer = CapturingAnswerModel()
    models.evaluator = CapturingEvaluatorModel()
    runtime = RagEvaluationRuntime(
        models=models,
        executor=ProviderExecutor(sleeper=lambda _: None),
    )

    with (
        patch.object(runtime_module, "run_rag_graph", return_value=initial),
        patch.object(runtime_module, "resume_rag_from_hitl", return_value=final),
    ):
        execution = runtime.execute_case(
            job_id="rag_eval_1",
            case=case,
            model_snapshot=TEST_MODEL_SNAPSHOT,
            timeout_seconds=30,
            cancellation=lambda: False,
        )

    assert execution.observation.route is RagRoute.SCOPE_SELECT
    assert "V2.1" in captured["answer"]
    assert "V2.1" in captured["judge"]
