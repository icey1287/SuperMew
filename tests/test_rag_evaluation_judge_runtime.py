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
    RagRoute,
)
from backend.evaluation.runtime import RagEvaluationRuntime
from backend.providers import ProviderExecutor
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
