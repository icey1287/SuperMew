import json
import math

import pytest
from pydantic import ValidationError

from backend.evaluation import (
    DatasetFingerprintMismatch,
    GateStatus,
    MetricDirection,
    ObservationCoverageError,
    RagEvalCase,
    RagEvalDataset,
    RagEvalGatePolicy,
    RagEvalObservation,
    RagEvalObservationBundle,
    RagExpectedBehavior,
    RagGoldChunk,
    RagGoldDocument,
    RagHitlKind,
    RagJudgeMetrics,
    RagMetricGate,
    RagProviderErrorStage,
    RagRetrievedChunk,
    dataset_fingerprint,
    evaluate_rag,
    evaluate_rag_partial,
    load_rag_eval_dataset,
    load_rag_eval_gates,
    load_rag_eval_observations,
    load_rag_eval_report,
    render_rag_eval_json,
    render_rag_eval_markdown,
)


def _answer_case(*, case_id: str = "case-1", critical: bool = False):
    return RagEvalCase(
        id=case_id,
        tags=("single_fact",),
        critical=critical,
        question="内部问题正文不应进入报告",
        expected=RagExpectedBehavior(
            complexity="simple",
            route="answer",
            outcome="ANSWERABLE",
        ),
        gold_documents=(RagGoldDocument(canonical_name="manual.pdf"),),
        gold_chunks=(
            RagGoldChunk(chunk_id="a"),
            RagGoldChunk(chunk_id="b"),
        ),
        reference_answer="参考答案",
        required_claims=("声明 A",),
        conflicts=("来源不存在冲突",),
    )


def _answer_observation(
    *,
    case_id: str = "case-1",
    chunk_ids=("a", "x", "b"),
    route: str = "answer",
    outcome: str = "ANSWERABLE",
    provider_error_code: str | None = None,
    duration_ms: float = 10,
    rewrite_performed: bool = True,
):
    return RagEvalObservation(
        case_id=case_id,
        complexity="simple",
        route=route,
        outcome=outcome,
        provider_error_code=provider_error_code,
        duration_ms=duration_ms,
        rewrite_performed=rewrite_performed,
        retrieved_chunks=tuple(
            RagRetrievedChunk(
                rank=rank,
                chunk_id=chunk_id,
                canonical_name="manual.pdf",
            )
            for rank, chunk_id in enumerate(chunk_ids, 1)
        ),
        initial_retrieved_chunks=(
            RagRetrievedChunk(rank=1, chunk_id="x", canonical_name="manual.pdf"),
        )
        if rewrite_performed
        else (),
        rewrite_retrieved_chunks=(
            RagRetrievedChunk(rank=1, chunk_id="a", canonical_name="manual.pdf"),
            RagRetrievedChunk(rank=2, chunk_id="b", canonical_name="manual.pdf"),
        )
        if rewrite_performed
        else (),
    )


def _dataset(case: RagEvalCase):
    return RagEvalDataset(name="rag_smoke_v1", cases=(case,))


def test_strict_schema_rejects_unknown_fields_and_duplicate_cases():
    payload = _dataset(_answer_case()).model_dump(mode="json")
    payload["cases"][0]["unknown"] = True

    with pytest.raises(ValidationError):
        RagEvalDataset.model_validate(payload)

    with pytest.raises(ValidationError, match="case ids must be unique"):
        RagEvalDataset(
            name="duplicate_cases",
            cases=(_answer_case(), _answer_case()),
        )


def test_observations_must_be_unique_and_cover_every_case():
    dataset = _dataset(_answer_case())
    observation = _answer_observation()
    gates = RagEvalGatePolicy(k_values=(3,))

    with pytest.raises(ObservationCoverageError, match="duplicate observation"):
        evaluate_rag(dataset, [observation, observation], gates)

    with pytest.raises(ObservationCoverageError, match="cover the dataset exactly"):
        evaluate_rag(dataset, [], gates)

    with pytest.raises(ValidationError, match="observation case ids must be unique"):
        RagEvalObservationBundle(
            dataset_fingerprint=dataset_fingerprint(dataset),
            observations=(observation, observation),
        )


def test_partial_report_can_preserve_a_crash_before_the_first_observation():
    dataset = _dataset(_answer_case())

    report = evaluate_rag_partial(
        dataset,
        [],
        RagEvalGatePolicy(k_values=(3,)),
        metadata={"failure": {"code": "INTERNAL_ERROR", "stage": "worker"}},
    )

    assert report.case_count == 1
    assert report.observation_count == 0
    assert report.cases == ()
    assert report.passed is False
    assert report.metadata["partial_report"] is True
    gate_status = {gate.name: gate.status.value for gate in report.gates}
    assert gate_status["observation_coverage"] == "failed"
    assert gate_status["job_execution"] == "failed"


def test_ranking_rewrite_route_outcome_hitl_and_latency_metrics_are_exact():
    dataset = _dataset(_answer_case())
    report = evaluate_rag(
        dataset,
        [_answer_observation()],
        RagEvalGatePolicy(k_values=(3,)),
        metadata={"profile": "offline"},
    )

    assert report.metrics["recall_at_3"].value == 1.0
    assert report.metrics["precision_at_3"].value == pytest.approx(2 / 3)
    assert report.metrics["mrr_at_3"].value == 1.0
    expected_ndcg = (1 + (1 / math.log2(4))) / (1 + (1 / math.log2(3)))
    assert report.metrics["ndcg_at_3"].value == pytest.approx(expected_ndcg)
    assert report.metrics["gold_chunk_coverage"].value == 1.0
    assert report.metrics["document_recall_at_3"].value == 1.0
    assert report.metrics["complexity_accuracy"].value == 1.0
    assert report.metrics["route_accuracy"].value == 1.0
    assert report.metrics["outcome_accuracy"].value == 1.0
    assert report.metrics["hitl_accuracy"].value == 1.0
    assert report.metrics["rewrite_recall_delta_at_3"].value == 1.0
    assert report.metrics["rewrite_improvement_rate_at_3"].value == 1.0
    assert report.metrics["provider_failure_rate"].value == 0.0
    assert report.metrics["latency_mean_ms"].value == 10.0
    assert report.metrics["latency_p50_ms"].value == 10.0
    assert report.metrics["latency_p95_ms"].value == 10.0
    assert report.slices["single_fact"].case_count == 1
    assert report.slices["single_fact"].metrics["recall_at_3"].value == 1.0
    assert report.metadata == {"profile": "offline"}
    assert report.unavailable_metrics["citation_precision"]


def test_evaluator_judge_metrics_are_aggregated_and_become_available():
    dataset = _dataset(_answer_case())
    observation = _answer_observation().model_copy(
        update={
            "judge": RagJudgeMetrics(
                answer_correctness=0.9,
                groundedness=0.8,
                answer_relevance=0.95,
                completeness=0.75,
                context_relevance=0.85,
                unsupported_claim_rate=0.1,
                conflict_disclosure_rate=1.0,
            )
        }
    )

    report = evaluate_rag(
        dataset,
        [observation],
        RagEvalGatePolicy(k_values=(3,)),
    )

    assert report.metrics["answer_correctness"].value == 0.9
    assert report.metrics["unsupported_claim_rate"].value == 0.1
    assert "answer_correctness" not in report.unavailable_metrics
    assert "groundedness" not in report.unavailable_metrics
    assert report.cases[0].checks["judge_groundedness"] is True
    assert report.cases[0].passed is True


def test_context_relevance_is_not_applicable_to_no_knowledge_without_context():
    case = RagEvalCase(
        id="no-knowledge",
        question="知识库之外的问题",
        expected=RagExpectedBehavior(
            route="no_knowledge",
            outcome="NO_KNOWLEDGE",
            acceptable_abstention=True,
        ),
    )
    observation = RagEvalObservation(
        case_id=case.id,
        route="no_knowledge",
        outcome="NO_KNOWLEDGE",
        duration_ms=1,
        judge=RagJudgeMetrics(
            answer_correctness=1,
            groundedness=1,
            answer_relevance=1,
            completeness=1,
            context_relevance=0,
            unsupported_claim_rate=0,
            conflict_disclosure_rate=1,
        ),
    )

    report = evaluate_rag(
        RagEvalDataset(name="no_knowledge", cases=(case,)),
        [observation],
        RagEvalGatePolicy(k_values=(5,)),
    )

    assert report.cases[0].checks["judge_context_relevance"] is None
    assert report.cases[0].metrics["context_relevance"] is None
    assert report.metrics["context_relevance"].eligible_cases == 0
    assert report.cases[0].passed is True


def test_versioned_retrieved_chunk_id_matches_stable_gold_chunk_id():
    dataset = _dataset(_answer_case())
    observation = _answer_observation(
        chunk_ids=(
            "docver_019f8::a",
            "docver_019f8::x",
            "docver_019f8::b",
        )
    )

    report = evaluate_rag(
        dataset,
        [observation],
        RagEvalGatePolicy(k_values=(3,)),
    )

    assert report.metrics["recall_at_3"].value == 1
    assert report.metrics["gold_chunk_coverage"].value == 1


def test_hitl_resolution_provider_failure_and_latency_percentiles():
    cases = (
        RagEvalCase(
            id="hitl",
            tags=("hitl",),
            critical=True,
            question="需要补充版本",
            expected=RagExpectedBehavior(
                route="clarify",
                outcome="INSUFFICIENT_EVIDENCE",
                hitl="clarify",
                hitl_resolution_success=True,
            ),
            hitl_answers=("v2",),
        ),
        RagEvalCase(
            id="healthy",
            question="普通行为检查",
            expected=RagExpectedBehavior(),
        ),
        RagEvalCase(
            id="failed",
            question="Provider 故障检查",
            expected=RagExpectedBehavior(),
        ),
    )
    observations = (
        RagEvalObservation(
            case_id="hitl",
            route="clarify",
            outcome="INSUFFICIENT_EVIDENCE",
            hitl=RagHitlKind.CLARIFY,
            hitl_resolution_success=True,
            duration_ms=10,
        ),
        RagEvalObservation(case_id="healthy", duration_ms=20),
        RagEvalObservation(
            case_id="failed",
            provider_error_code="VECTOR_STORE_UNAVAILABLE",
            duration_ms=30,
        ),
    )

    report = evaluate_rag(
        RagEvalDataset(name="behavior", cases=cases),
        observations,
        RagEvalGatePolicy(k_values=(5,)),
    )

    assert report.metrics["hitl_accuracy"].value == 1.0
    assert report.metrics["hitl_resolution_success_rate"].value == 1.0
    assert report.metrics["provider_failure_rate"].value == pytest.approx(1 / 3)
    assert report.metrics["latency_mean_ms"].value == 20.0
    assert report.metrics["latency_p50_ms"].value == 20.0
    assert report.metrics["latency_p95_ms"].value == 30.0


def test_current_critical_failure_is_blocked_without_a_baseline():
    case = RagEvalCase(
        id="critical",
        critical=True,
        question="critical question",
        expected=RagExpectedBehavior(route="answer"),
    )
    report = evaluate_rag(
        RagEvalDataset(name="critical_dataset", cases=(case,)),
        [
            RagEvalObservation(
                case_id="critical",
                route="rewrite",
                duration_ms=1,
            )
        ],
        RagEvalGatePolicy(k_values=(10,), critical_no_regression=True),
    )

    assert report.passed is False
    assert report.gates[0].status is GateStatus.FAILED
    assert "critical" in report.gates[0].detail


def test_dataset_fingerprint_mismatch_is_rejected_for_bundle_and_baseline():
    dataset = _dataset(_answer_case())
    other_dataset = RagEvalDataset(
        name="rag_smoke_v1",
        cases=(_answer_case(case_id="case-2"),),
    )
    gates = RagEvalGatePolicy(k_values=(3,))
    baseline = evaluate_rag(dataset, [_answer_observation()], gates)
    mismatched_bundle = RagEvalObservationBundle(
        dataset_fingerprint=dataset_fingerprint(other_dataset),
        observations=(_answer_observation(),),
    )

    with pytest.raises(DatasetFingerprintMismatch, match="observation"):
        evaluate_rag(dataset, mismatched_bundle, gates)

    with pytest.raises(DatasetFingerprintMismatch, match="baseline"):
        evaluate_rag(
            other_dataset,
            [_answer_observation(case_id="case-2")],
            gates,
            baseline=baseline,
        )


def test_critical_and_per_metric_regressions_fail_gates():
    dataset = _dataset(_answer_case(critical=True))
    gates = RagEvalGatePolicy(
        k_values=(3,),
        metric_gates=(
            RagMetricGate(
                metric="recall_at_3",
                direction=MetricDirection.HIGHER_IS_BETTER,
                minimum=0.5,
                max_regression=0.1,
            ),
            RagMetricGate(
                metric="provider_failure_rate",
                direction=MetricDirection.LOWER_IS_BETTER,
                maximum=0.0,
                max_regression=0.0,
            ),
        ),
    )
    baseline = evaluate_rag(dataset, [_answer_observation()], gates)
    candidate = evaluate_rag(
        dataset,
        [
            _answer_observation(
                chunk_ids=("x",),
                route="no_knowledge",
                outcome="NO_KNOWLEDGE",
                provider_error_code="VECTOR_STORE_UNAVAILABLE",
            )
        ],
        gates,
        baseline=baseline,
    )

    assert not candidate.passed
    gate_status = {gate.name: gate.status.value for gate in candidate.gates}
    assert gate_status["critical_no_regression"] == "failed"
    assert gate_status["metric:recall_at_3"] == "failed"
    assert gate_status["metric:provider_failure_rate"] == "failed"
    assert "case-1:route" in candidate.gates[0].detail


def test_absolute_gate_threshold_is_not_overwritten_by_baseline_threshold():
    dataset = _dataset(_answer_case())
    gates = RagEvalGatePolicy(
        k_values=(3,),
        metric_gates=(
            RagMetricGate(
                metric="latency_p95_ms",
                direction=MetricDirection.LOWER_IS_BETTER,
                maximum=1,
                max_regression=0,
            ),
        ),
    )
    baseline = evaluate_rag(
        dataset,
        [_answer_observation(duration_ms=35_890)],
        gates,
    )

    candidate = evaluate_rag(
        dataset,
        [_answer_observation(duration_ms=10)],
        gates,
        baseline=baseline,
    )

    gate = next(item for item in candidate.gates if item.metric == "latency_p95_ms")
    assert gate.status is GateStatus.FAILED
    assert gate.threshold == 1
    assert gate.baseline_threshold == 35_890


def test_provider_error_stage_is_part_of_observation_contract():
    observation = RagEvalObservation(
        case_id="failed",
        provider_error_code="MODEL_TIMEOUT",
        provider_error_stage=RagProviderErrorStage.GENERATION,
        duration_ms=1,
    )

    assert observation.model_dump(mode="json")["provider_error_stage"] == "generation"


def test_load_helpers_and_renderers_are_deterministic_and_redacted(tmp_path):
    dataset = _dataset(_answer_case())
    gates = RagEvalGatePolicy(k_values=(3,))
    bundle = RagEvalObservationBundle(
        dataset_fingerprint=dataset_fingerprint(dataset),
        observations=(_answer_observation(),),
    )
    report = evaluate_rag(dataset, bundle, gates)

    dataset_path = tmp_path / "dataset.json"
    gates_path = tmp_path / "gates.json"
    observations_path = tmp_path / "observations.json"
    report_path = tmp_path / "report.json"
    dataset_path.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    gates_path.write_text(gates.model_dump_json(indent=2), encoding="utf-8")
    observations_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    rendered = render_rag_eval_json(report)
    report_path.write_text(rendered, encoding="utf-8")

    assert load_rag_eval_dataset(dataset_path) == dataset
    assert load_rag_eval_gates(gates_path) == gates
    assert load_rag_eval_observations(observations_path) == bundle
    assert load_rag_eval_report(report_path) == report
    assert render_rag_eval_json(report) == rendered
    assert json.loads(rendered)["passed"] is True

    markdown = render_rag_eval_markdown(report)
    assert markdown == render_rag_eval_markdown(report)
    assert "citation_precision" in markdown
    assert "内部问题正文不应进入报告" not in rendered
    assert "内部问题正文不应进入报告" not in markdown
