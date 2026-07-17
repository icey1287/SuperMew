from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from enum import StrEnum
from pathlib import Path
from collections.abc import Iterable
from typing import Annotated, Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProviderCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class RagEvaluationError(ValueError):
    """Base error for invalid evaluation inputs or incomparable reports."""


class DatasetFingerprintMismatch(RagEvaluationError):
    """Raised when observations or a baseline belong to another dataset."""


class ObservationCoverageError(RagEvaluationError):
    """Raised when observations do not cover the dataset exactly once."""


class StrictEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RagComplexity(StrEnum):
    SIMPLE = "simple"
    COMPLEX = "complex"


class RagRoute(StrEnum):
    ANSWER = "answer"
    REWRITE = "rewrite"
    CLARIFY = "clarify"
    SCOPE_SELECT = "scope_select"
    NO_KNOWLEDGE = "no_knowledge"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    PROVIDER_FAILED = "provider_failed"


class RagOutcome(StrEnum):
    ANSWERABLE = "ANSWERABLE"
    NO_KNOWLEDGE = "NO_KNOWLEDGE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RagHitlKind(StrEnum):
    NONE = "none"
    CLARIFY = "clarify"
    SCOPE_SELECT = "scope_select"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RagExpectedBehavior(StrictEvalModel):
    complexity: RagComplexity | None = None
    route: RagRoute | None = None
    outcome: RagOutcome | None = None
    hitl: RagHitlKind = RagHitlKind.NONE
    acceptable_abstention: bool = False
    hitl_resolution_success: bool | None = None
    hitl_final_outcome: RagOutcome | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> RagExpectedBehavior:
        route_for_hitl = {
            RagHitlKind.CLARIFY: RagRoute.CLARIFY,
            RagHitlKind.SCOPE_SELECT: RagRoute.SCOPE_SELECT,
        }
        expected_hitl_route = route_for_hitl.get(self.hitl)
        if expected_hitl_route is not None and self.route not in {
            None,
            expected_hitl_route,
        }:
            raise ValueError("expected HITL kind and route disagree")
        if self.hitl is RagHitlKind.NONE and self.route in {
            RagRoute.CLARIFY,
            RagRoute.SCOPE_SELECT,
        }:
            raise ValueError("HITL route requires a non-none HITL kind")
        if self.hitl is RagHitlKind.NONE and self.hitl_resolution_success is not None:
            raise ValueError("HITL resolution expectation requires HITL")
        if self.hitl is RagHitlKind.NONE and self.hitl_final_outcome is not None:
            raise ValueError("HITL final outcome expectation requires HITL")
        if self.outcome is RagOutcome.ANSWERABLE and self.acceptable_abstention:
            raise ValueError("answerable cases cannot accept abstention")
        if self.outcome is RagOutcome.NO_KNOWLEDGE and not self.acceptable_abstention:
            raise ValueError("no-knowledge cases must accept abstention")
        return self


class RagGoldDocument(StrictEvalModel):
    document_id: NonEmptyText | None = None
    canonical_name: NonEmptyText | None = None

    @model_validator(mode="after")
    def require_identity(self) -> RagGoldDocument:
        if self.document_id is None and self.canonical_name is None:
            raise ValueError("gold document requires document_id or canonical_name")
        return self


class RagGoldChunk(StrictEvalModel):
    chunk_id: NonEmptyText | None = None
    content_sha256: Sha256 | None = None

    @field_validator("content_sha256", mode="before")
    @classmethod
    def normalize_hash(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_identity(self) -> RagGoldChunk:
        if self.chunk_id is None and self.content_sha256 is None:
            raise ValueError("gold chunk requires chunk_id or content_sha256")
        return self


class RagEvalCase(StrictEvalModel):
    id: Identifier
    tags: tuple[Identifier, ...] = ()
    critical: bool = False
    question: NonEmptyText
    expected: RagExpectedBehavior
    gold_documents: tuple[RagGoldDocument, ...] = ()
    gold_chunks: tuple[RagGoldChunk, ...] = ()
    reference_answer: NonEmptyText | None = None
    required_claims: tuple[NonEmptyText, ...] = ()
    conflicts: tuple[NonEmptyText, ...] = ()
    hitl_answers: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_case(self) -> RagEvalCase:
        _require_unique(self.tags, "case tags")
        _require_unique(
            (item.document_id for item in self.gold_documents if item.document_id),
            "gold document ids",
        )
        _require_unique(
            (
                _normalize_name(item.canonical_name)
                for item in self.gold_documents
                if item.canonical_name
            ),
            "gold document names",
        )
        _require_unique(
            (item.chunk_id for item in self.gold_chunks if item.chunk_id),
            "gold chunk ids",
        )
        _require_unique(
            (item.content_sha256 for item in self.gold_chunks if item.content_sha256),
            "gold chunk hashes",
        )
        _require_unique(self.required_claims, "required claims")
        _require_unique(self.conflicts, "conflicts")
        if self.expected.outcome is RagOutcome.ANSWERABLE and not (
            self.gold_chunks or self.gold_documents
        ):
            raise ValueError("answerable cases require gold evidence")
        if self.expected.outcome is RagOutcome.NO_KNOWLEDGE and (
            self.gold_chunks or self.gold_documents
        ):
            raise ValueError("no-knowledge cases cannot contain gold evidence")
        if self.hitl_answers and self.expected.hitl is RagHitlKind.NONE:
            raise ValueError("HITL answers require a non-none HITL expectation")
        return self


class RagEvalDataset(StrictEvalModel):
    schema_version: Literal[1] = 1
    name: Identifier
    cases: tuple[RagEvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> RagEvalDataset:
        _require_unique((case.id for case in self.cases), "case ids")
        return self


class RagRetrievedChunk(StrictEvalModel):
    rank: int = Field(ge=1)
    chunk_id: NonEmptyText | None = None
    content_sha256: Sha256 | None = None
    document_id: NonEmptyText | None = None
    canonical_name: NonEmptyText | None = None
    merged_from_children: bool = False

    @field_validator("content_sha256", mode="before")
    @classmethod
    def normalize_hash(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_chunk_identity(self) -> RagRetrievedChunk:
        if self.chunk_id is None and self.content_sha256 is None:
            raise ValueError("retrieved chunk requires chunk_id or content_sha256")
        return self


class RagJudgeMetrics(StrictEvalModel):
    answer_correctness: float = Field(ge=0, le=1, allow_inf_nan=False)
    groundedness: float = Field(ge=0, le=1, allow_inf_nan=False)
    answer_relevance: float = Field(ge=0, le=1, allow_inf_nan=False)
    completeness: float = Field(ge=0, le=1, allow_inf_nan=False)
    context_relevance: float = Field(ge=0, le=1, allow_inf_nan=False)
    unsupported_claim_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    conflict_disclosure_rate: float = Field(ge=0, le=1, allow_inf_nan=False)


class RagEvalObservation(StrictEvalModel):
    case_id: Identifier
    complexity: RagComplexity | None = None
    route: RagRoute | None = None
    outcome: RagOutcome | None = None
    hitl: RagHitlKind = RagHitlKind.NONE
    hitl_resolution_success: bool | None = None
    hitl_final_outcome: RagOutcome | None = None
    rewrite_performed: bool = False
    provider_error_code: ProviderCode | None = None
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    judge: RagJudgeMetrics | None = None
    retrieved_chunks: tuple[RagRetrievedChunk, ...] = ()
    initial_retrieved_chunks: tuple[RagRetrievedChunk, ...] = ()
    rewrite_retrieved_chunks: tuple[RagRetrievedChunk, ...] = ()

    @model_validator(mode="after")
    def validate_rankings(self) -> RagEvalObservation:
        for name, chunks in (
            ("retrieved_chunks", self.retrieved_chunks),
            ("initial_retrieved_chunks", self.initial_retrieved_chunks),
            ("rewrite_retrieved_chunks", self.rewrite_retrieved_chunks),
        ):
            _require_unique((chunk.rank for chunk in chunks), f"{name} ranks")
            _require_unique(
                (chunk.chunk_id for chunk in chunks if chunk.chunk_id),
                f"{name} chunk ids",
            )
            _require_unique(
                (chunk.content_sha256 for chunk in chunks if chunk.content_sha256),
                f"{name} content hashes",
            )
        if not self.rewrite_performed and self.rewrite_retrieved_chunks:
            raise ValueError("rewrite chunks require rewrite_performed=true")
        if self.hitl is RagHitlKind.NONE and self.hitl_resolution_success is not None:
            raise ValueError("HITL resolution observation requires HITL")
        if self.hitl is RagHitlKind.NONE and self.hitl_final_outcome is not None:
            raise ValueError("HITL final outcome observation requires HITL")
        return self


class RagEvalObservationBundle(StrictEvalModel):
    schema_version: Literal[1] = 1
    dataset_fingerprint: Sha256
    observations: tuple[RagEvalObservation, ...]

    @model_validator(mode="after")
    def observation_ids_are_unique(self) -> RagEvalObservationBundle:
        _require_unique(
            (observation.case_id for observation in self.observations),
            "observation case ids",
        )
        return self


class RagMetricGate(StrictEvalModel):
    metric: Identifier
    direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    max_regression: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    required: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> RagMetricGate:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("metric gate minimum cannot exceed maximum")
        return self


class RagEvalGatePolicy(StrictEvalModel):
    schema_version: Literal[1] = 1
    k_values: tuple[int, ...] = (5, 10)
    critical_no_regression: bool = True
    required_provenance: Literal["contract_smoke", "live_rag"] | None = None
    metric_gates: tuple[RagMetricGate, ...] = ()

    @field_validator("k_values")
    @classmethod
    def validate_k_values(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if not values or any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("k_values must contain positive integers")
        if len(set(values)) != len(values):
            raise ValueError("k_values must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def metric_gates_are_unique(self) -> RagEvalGatePolicy:
        _require_unique((gate.metric for gate in self.metric_gates), "metric gates")
        return self


class RagMetricResult(StrictEvalModel):
    value: float | None = Field(allow_inf_nan=False)
    eligible_cases: int = Field(ge=0)


class RagEvalSliceResult(StrictEvalModel):
    case_count: int = Field(ge=1)
    metrics: dict[str, RagMetricResult]


class RagEvalCaseResult(StrictEvalModel):
    case_id: Identifier
    critical: bool
    metrics: dict[str, float | None]
    checks: dict[str, bool | None]
    provider_failed: bool
    gold_chunk_count: int = Field(ge=0)
    matched_gold_chunk_count: int = Field(ge=0)
    passed: bool

    @field_validator("metrics")
    @classmethod
    def metrics_are_finite(
        cls, values: dict[str, float | None]
    ) -> dict[str, float | None]:
        if any(
            value is not None and not math.isfinite(value) for value in values.values()
        ):
            raise ValueError("case metrics must be finite")
        return values


class RagGateResult(StrictEvalModel):
    name: NonEmptyText
    status: GateStatus
    metric: str | None = None
    actual: float | None = Field(default=None, allow_inf_nan=False)
    baseline: float | None = Field(default=None, allow_inf_nan=False)
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    detail: str = ""


class RagEvalReport(StrictEvalModel):
    schema_version: Literal[1] = 1
    dataset_name: Identifier
    dataset_fingerprint: Sha256
    case_count: int = Field(ge=1)
    observation_count: int = Field(ge=0)
    metrics: dict[str, RagMetricResult]
    slices: dict[Identifier, RagEvalSliceResult]
    unavailable_metrics: dict[str, NonEmptyText]
    cases: tuple[RagEvalCaseResult, ...]
    gates: tuple[RagGateResult, ...]
    passed: bool
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_is_json_safe(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _canonical_json(value)
        return value


_UNAVAILABLE_METRICS = {
    "answer_correctness": "stable answer-judge Interface is not available",
    "groundedness": "stable claim-to-evidence Interface is not available",
    "answer_relevance": "stable answer-judge Interface is not available",
    "completeness": "stable answer-judge Interface is not available",
    "context_relevance": "stable answer-judge Interface is not available",
    "parent_expansion_precision": (
        "retrieved parent chunks do not yet expose stable child lineage"
    ),
    "citation_precision": "stable citation identity Interface is not available",
    "citation_recall": "stable citation identity Interface is not available",
    "unsupported_claim_rate": "structured answer claims are not available",
    "conflict_disclosure_rate": "structured conflict claims are not available",
}

_JUDGE_METRICS = (
    "answer_correctness",
    "groundedness",
    "answer_relevance",
    "completeness",
    "context_relevance",
    "unsupported_claim_rate",
    "conflict_disclosure_rate",
)


def load_rag_eval_dataset(path: str | Path) -> RagEvalDataset:
    return RagEvalDataset.model_validate(_load_json(path))


def load_rag_eval_observations(path: str | Path) -> RagEvalObservationBundle:
    return RagEvalObservationBundle.model_validate(_load_json(path))


def load_rag_eval_gates(path: str | Path) -> RagEvalGatePolicy:
    return RagEvalGatePolicy.model_validate(_load_json(path))


def load_rag_eval_report(path: str | Path) -> RagEvalReport:
    return RagEvalReport.model_validate(_load_json(path))


def dataset_fingerprint(dataset: RagEvalDataset) -> str:
    payload = _canonical_json(dataset.model_dump(mode="json"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_rag(
    dataset: RagEvalDataset,
    observations: Sequence[RagEvalObservation] | RagEvalObservationBundle,
    gates: RagEvalGatePolicy,
    baseline: RagEvalReport | None = None,
    metadata: dict[str, JsonValue] | None = None,
) -> RagEvalReport:
    fingerprint = dataset_fingerprint(dataset)
    observation_values: Sequence[RagEvalObservation]
    if isinstance(observations, RagEvalObservationBundle):
        if observations.dataset_fingerprint != fingerprint:
            raise DatasetFingerprintMismatch(
                "observation dataset fingerprint does not match the dataset"
            )
        observation_values = observations.observations
    else:
        observation_values = observations

    if baseline is not None and baseline.dataset_fingerprint != fingerprint:
        raise DatasetFingerprintMismatch(
            "baseline dataset fingerprint does not match the dataset"
        )

    case_index = {case.id: case for case in dataset.cases}
    observation_index = _index_observations(observation_values)
    if set(case_index) != set(observation_index):
        missing = sorted(set(case_index) - set(observation_index))
        unknown = sorted(set(observation_index) - set(case_index))
        raise ObservationCoverageError(
            f"observations must cover the dataset exactly; missing={missing}, unknown={unknown}"
        )

    case_results = tuple(
        _score_case(case_index[case_id], observation_index[case_id], gates.k_values)
        for case_id in sorted(case_index)
    )
    metrics = _aggregate_metrics(case_results, observation_index, gates.k_values)
    slices = _aggregate_tag_slices(
        dataset=dataset,
        cases=case_results,
        observations=observation_index,
        k_values=gates.k_values,
    )

    baseline_cases: dict[str, RagEvalCaseResult] = {}
    if baseline is not None:
        baseline_cases = {item.case_id: item for item in baseline.cases}
        if set(baseline_cases) != set(case_index):
            raise ObservationCoverageError(
                "baseline cases do not cover the current dataset exactly"
            )

    resolved_metadata = metadata or {}
    gate_results = _evaluate_gates(
        policy=gates,
        metrics=metrics,
        cases=case_results,
        baseline=baseline,
        baseline_cases=baseline_cases,
        metadata=resolved_metadata,
    )
    passed = all(result.status is not GateStatus.FAILED for result in gate_results)
    unavailable_metrics = dict(_UNAVAILABLE_METRICS)
    for metric_name in _JUDGE_METRICS:
        metric_result = metrics.get(metric_name)
        if metric_result is not None and metric_result.eligible_cases:
            unavailable_metrics.pop(metric_name, None)
    return RagEvalReport(
        dataset_name=dataset.name,
        dataset_fingerprint=fingerprint,
        case_count=len(dataset.cases),
        observation_count=len(observation_values),
        metrics={key: metrics[key] for key in sorted(metrics)},
        slices=slices,
        unavailable_metrics=dict(sorted(unavailable_metrics.items())),
        cases=case_results,
        gates=tuple(gate_results),
        passed=passed,
        metadata=resolved_metadata,
    )


def render_rag_eval_json(report: RagEvalReport) -> str:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def render_rag_eval_markdown(report: RagEvalReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [
        f"# RAG Evaluation: {status}",
        "",
        f"- Dataset: `{report.dataset_name}`",
        f"- Fingerprint: `{report.dataset_fingerprint}`",
        f"- Cases: {report.case_count}",
        "",
        "## Metrics",
        "",
        "| Metric | Value | Eligible cases |",
        "| --- | ---: | ---: |",
    ]
    for name, metric in sorted(report.metrics.items()):
        lines.append(
            f"| `{name}` | {_format_number(metric.value)} | {metric.eligible_cases} |"
        )

    lines.extend(
        [
            "",
            "## Tag slices",
            "",
            "| Tag | Cases | Case pass rate | Outcome accuracy |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for tag, slice_result in sorted(report.slices.items()):
        case_pass_rate = slice_result.metrics.get("case_pass_rate")
        outcome_accuracy = slice_result.metrics.get("outcome_accuracy")
        lines.append(
            "| "
            f"`{tag}` | {slice_result.case_count} | "
            f"{_format_number(case_pass_rate.value if case_pass_rate else None)} | "
            f"{_format_number(outcome_accuracy.value if outcome_accuracy else None)} |"
        )

    lines.extend(
        [
            "",
            "## Gates",
            "",
            "| Gate | Status | Actual | Baseline | Detail |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for gate in report.gates:
        detail = gate.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(
            "| "
            f"`{gate.name}` | {gate.status.value} | {_format_number(gate.actual)} | "
            f"{_format_number(gate.baseline)} | {detail} |"
        )

    failed_cases = [case.case_id for case in report.cases if not case.passed]
    lines.extend(["", "## Failed cases", ""])
    if failed_cases:
        lines.extend(f"- `{case_id}`" for case_id in failed_cases)
    else:
        lines.append("None.")

    lines.extend(["", "## Unavailable metrics", ""])
    lines.extend(
        f"- `{name}`: {reason}"
        for name, reason in sorted(report.unavailable_metrics.items())
    )
    return "\n".join(lines) + "\n"


def _load_json(path: str | Path) -> Any:
    def reject_constant(value: str):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=reject_constant,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _require_unique(values: Iterable[Any], label: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} must be unique")


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFC", " ".join(value.split()))


def _chunk_matches(gold: RagGoldChunk, retrieved: RagRetrievedChunk) -> bool:
    return bool(
        (gold.chunk_id is not None and gold.chunk_id == retrieved.chunk_id)
        or (
            gold.content_sha256 is not None
            and gold.content_sha256 == retrieved.content_sha256
        )
    )


def _document_matches(
    gold: RagGoldDocument,
    retrieved: RagRetrievedChunk,
) -> bool:
    return bool(
        (gold.document_id is not None and gold.document_id == retrieved.document_id)
        or (
            gold.canonical_name is not None
            and _normalize_name(gold.canonical_name)
            == _normalize_name(retrieved.canonical_name)
        )
    )


def _ranked(chunks: Sequence[RagRetrievedChunk], k: int | None = None):
    ordered = sorted(chunks, key=lambda item: item.rank)
    if k is None:
        return ordered
    return [item for item in ordered if item.rank <= k]


def _chunk_relevance(
    gold_chunks: Sequence[RagGoldChunk],
    retrieved_chunks: Sequence[RagRetrievedChunk],
) -> tuple[list[int], int]:
    unmatched = set(range(len(gold_chunks)))
    relevance: list[int] = []
    for retrieved in retrieved_chunks:
        match = next(
            (
                index
                for index in sorted(unmatched)
                if _chunk_matches(gold_chunks[index], retrieved)
            ),
            None,
        )
        if match is None:
            relevance.append(0)
            continue
        unmatched.remove(match)
        relevance.append(1)
    return relevance, len(gold_chunks) - len(unmatched)


def _document_match_count(
    gold_documents: Sequence[RagGoldDocument],
    retrieved_chunks: Sequence[RagRetrievedChunk],
) -> int:
    unmatched = set(range(len(gold_documents)))
    seen: set[tuple[str | None, str | None]] = set()
    for retrieved in retrieved_chunks:
        identity = retrieved.document_id, _normalize_name(retrieved.canonical_name)
        if identity in seen:
            continue
        seen.add(identity)
        match = next(
            (
                index
                for index in sorted(unmatched)
                if _document_matches(gold_documents[index], retrieved)
            ),
            None,
        )
        if match is not None:
            unmatched.remove(match)
    return len(gold_documents) - len(unmatched)


def _ranking_metrics(
    gold_chunks: Sequence[RagGoldChunk],
    retrieved_chunks: Sequence[RagRetrievedChunk],
    k: int,
) -> dict[str, float]:
    ranked = _ranked(retrieved_chunks, k)
    relevance, matched = _chunk_relevance(gold_chunks, ranked)
    recall = matched / len(gold_chunks)
    precision = matched / k
    first_relevant_rank = next(
        (chunk.rank for chunk, relevant in zip(ranked, relevance) if relevant),
        None,
    )
    mrr = 0.0 if first_relevant_rank is None else 1.0 / first_relevant_rank
    dcg = sum(
        relevant / math.log2(chunk.rank + 1)
        for chunk, relevant in zip(ranked, relevance)
    )
    ideal_hits = min(len(gold_chunks), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
    return {
        "recall": _round(recall),
        "precision": _round(precision),
        "mrr": _round(mrr),
        "ndcg": _round(ndcg),
    }


def _score_case(
    case: RagEvalCase,
    observation: RagEvalObservation,
    k_values: Sequence[int],
) -> RagEvalCaseResult:
    metrics: dict[str, float | None] = {}
    checks: dict[str, bool | None] = {
        "complexity": _optional_equal(
            case.expected.complexity,
            observation.complexity,
        ),
        "route": _optional_equal(case.expected.route, observation.route),
        "outcome": _optional_equal(case.expected.outcome, observation.outcome),
        "hitl": observation.hitl == case.expected.hitl,
        "hitl_resolution": _optional_equal(
            case.expected.hitl_resolution_success,
            observation.hitl_resolution_success,
        ),
        "hitl_final_outcome": _optional_equal(
            case.expected.hitl_final_outcome,
            observation.hitl_final_outcome,
        ),
    }

    matched_all = 0
    if case.gold_chunks:
        _, matched_all = _chunk_relevance(
            case.gold_chunks,
            _ranked(observation.retrieved_chunks),
        )
        metrics["gold_chunk_coverage"] = _round(matched_all / len(case.gold_chunks))
        for k in k_values:
            ranking = _ranking_metrics(
                case.gold_chunks,
                observation.retrieved_chunks,
                k,
            )
            for metric_name, value in ranking.items():
                metrics[f"{metric_name}_at_{k}"] = value
        largest_k = max(k_values)
        checks[f"retrieval_hit_at_{largest_k}"] = bool(
            metrics[f"recall_at_{largest_k}"]
        )
    else:
        metrics["gold_chunk_coverage"] = None
        for k in k_values:
            for metric_name in ("recall", "precision", "mrr", "ndcg"):
                metrics[f"{metric_name}_at_{k}"] = None
        checks[f"retrieval_hit_at_{max(k_values)}"] = None

    if case.gold_documents:
        for k in k_values:
            matched_documents = _document_match_count(
                case.gold_documents,
                _ranked(observation.retrieved_chunks, k),
            )
            metrics[f"document_recall_at_{k}"] = _round(
                matched_documents / len(case.gold_documents)
            )
    else:
        for k in k_values:
            metrics[f"document_recall_at_{k}"] = None

    largest_k = max(k_values)
    if observation.rewrite_performed and case.gold_chunks:
        initial = _ranking_metrics(
            case.gold_chunks,
            observation.initial_retrieved_chunks,
            largest_k,
        )["recall"]
        final = _ranking_metrics(
            case.gold_chunks,
            observation.rewrite_retrieved_chunks,
            largest_k,
        )["recall"]
        delta = _round(final - initial)
        metrics[f"rewrite_recall_delta_at_{largest_k}"] = delta
        metrics[f"rewrite_improved_at_{largest_k}"] = float(delta > 0)
    else:
        metrics[f"rewrite_recall_delta_at_{largest_k}"] = None
        metrics[f"rewrite_improved_at_{largest_k}"] = None

    metrics["parent_expansion_precision"] = None

    judge = observation.judge
    judge_thresholds = {
        "answer_correctness": (0.5, True),
        "groundedness": (0.5, True),
        "answer_relevance": (0.5, True),
        "completeness": (0.5, True),
        "context_relevance": (0.5, True),
        "unsupported_claim_rate": (0.5, False),
        "conflict_disclosure_rate": (0.5, True),
    }
    for metric_name, (threshold, higher_is_better) in judge_thresholds.items():
        value = getattr(judge, metric_name) if judge is not None else None
        metrics[metric_name] = _round(value) if value is not None else None
        if value is None:
            checks[f"judge_{metric_name}"] = None
        elif higher_is_better:
            checks[f"judge_{metric_name}"] = value >= threshold
        else:
            checks[f"judge_{metric_name}"] = value <= threshold

    metrics["duration_ms"] = _round(observation.duration_ms)
    metrics["provider_failed"] = float(observation.provider_error_code is not None)
    applicable_checks = [value for value in checks.values() if value is not None]
    passed = all(applicable_checks) and observation.provider_error_code is None
    return RagEvalCaseResult(
        case_id=case.id,
        critical=case.critical,
        metrics={key: metrics[key] for key in sorted(metrics)},
        checks={key: checks[key] for key in sorted(checks)},
        provider_failed=observation.provider_error_code is not None,
        gold_chunk_count=len(case.gold_chunks),
        matched_gold_chunk_count=matched_all,
        passed=passed,
    )


def _aggregate_metrics(
    cases: Sequence[RagEvalCaseResult],
    observations: dict[str, RagEvalObservation],
    k_values: Sequence[int],
) -> dict[str, RagMetricResult]:
    names = sorted({name for case in cases for name in case.metrics})
    aggregated: dict[str, RagMetricResult] = {}
    for name in names:
        if name in {"duration_ms", "provider_failed"}:
            continue
        values = [
            case.metrics[name] for case in cases if case.metrics.get(name) is not None
        ]
        aggregated[name] = RagMetricResult(
            value=_mean(values),
            eligible_cases=len(values),
        )

    gold_total = sum(case.gold_chunk_count for case in cases)
    matched_total = sum(case.matched_gold_chunk_count for case in cases)
    aggregated["gold_chunk_coverage"] = RagMetricResult(
        value=_round(matched_total / gold_total) if gold_total else None,
        eligible_cases=sum(case.gold_chunk_count > 0 for case in cases),
    )

    for check_name, metric_name in (
        ("complexity", "complexity_accuracy"),
        ("route", "route_accuracy"),
        ("outcome", "outcome_accuracy"),
        ("hitl", "hitl_accuracy"),
        ("hitl_resolution", "hitl_resolution_success_rate"),
        ("hitl_final_outcome", "hitl_final_outcome_accuracy"),
    ):
        values = [
            float(case.checks[check_name])
            for case in cases
            if case.checks.get(check_name) is not None
        ]
        aggregated[metric_name] = RagMetricResult(
            value=_mean(values),
            eligible_cases=len(values),
        )

    durations = sorted(observation.duration_ms for observation in observations.values())
    aggregated["latency_mean_ms"] = RagMetricResult(
        value=_mean(durations), eligible_cases=len(durations)
    )
    aggregated["latency_p50_ms"] = RagMetricResult(
        value=_percentile(durations, 0.50), eligible_cases=len(durations)
    )
    aggregated["latency_p95_ms"] = RagMetricResult(
        value=_percentile(durations, 0.95), eligible_cases=len(durations)
    )
    failure_values = [
        float(observation.provider_error_code is not None)
        for observation in observations.values()
    ]
    aggregated["provider_failure_rate"] = RagMetricResult(
        value=_mean(failure_values), eligible_cases=len(failure_values)
    )
    aggregated["case_pass_rate"] = RagMetricResult(
        value=_mean([float(case.passed) for case in cases]),
        eligible_cases=len(cases),
    )

    largest_k = max(k_values)
    delta_name = f"rewrite_recall_delta_at_{largest_k}"
    improved_name = f"rewrite_improved_at_{largest_k}"
    delta_values = [
        case.metrics[delta_name]
        for case in cases
        if case.metrics.get(delta_name) is not None
    ]
    improved_values = [
        case.metrics[improved_name]
        for case in cases
        if case.metrics.get(improved_name) is not None
    ]
    aggregated[f"rewrite_improvement_rate_at_{largest_k}"] = RagMetricResult(
        value=_mean(improved_values),
        eligible_cases=len(improved_values),
    )
    aggregated[delta_name] = RagMetricResult(
        value=_mean(delta_values),
        eligible_cases=len(delta_values),
    )
    return aggregated


def _aggregate_tag_slices(
    *,
    dataset: RagEvalDataset,
    cases: Sequence[RagEvalCaseResult],
    observations: dict[str, RagEvalObservation],
    k_values: Sequence[int],
) -> dict[str, RagEvalSliceResult]:
    case_results = {case.case_id: case for case in cases}
    tags = sorted({tag for case in dataset.cases for tag in case.tags})
    slices: dict[str, RagEvalSliceResult] = {}
    for tag in tags:
        case_ids = tuple(sorted(case.id for case in dataset.cases if tag in case.tags))
        tagged_cases = tuple(case_results[case_id] for case_id in case_ids)
        tagged_observations = {case_id: observations[case_id] for case_id in case_ids}
        metrics = _aggregate_metrics(
            tagged_cases,
            tagged_observations,
            k_values,
        )
        slices[tag] = RagEvalSliceResult(
            case_count=len(case_ids),
            metrics={key: metrics[key] for key in sorted(metrics)},
        )
    return slices


def _evaluate_gates(
    *,
    policy: RagEvalGatePolicy,
    metrics: dict[str, RagMetricResult],
    cases: Sequence[RagEvalCaseResult],
    baseline: RagEvalReport | None,
    baseline_cases: dict[str, RagEvalCaseResult],
    metadata: dict[str, JsonValue],
) -> list[RagGateResult]:
    results: list[RagGateResult] = []
    if policy.required_provenance is not None:
        actual_provenance = metadata.get("provenance")
        matches = actual_provenance == policy.required_provenance
        results.append(
            RagGateResult(
                name="required_provenance",
                status=GateStatus.PASSED if matches else GateStatus.FAILED,
                detail=(
                    f"provenance={actual_provenance}"
                    if matches
                    else (
                        f"requires {policy.required_provenance}, "
                        f"got {actual_provenance}"
                    )
                ),
            )
        )
    if policy.critical_no_regression:
        current_failures = sorted(
            current.case_id
            for current in cases
            if current.critical and not current.passed
        )
        if baseline is None:
            results.append(
                RagGateResult(
                    name="critical_no_regression",
                    status=(
                        GateStatus.FAILED if current_failures else GateStatus.PASSED
                    ),
                    detail=(
                        "critical cases currently failing: "
                        + ", ".join(current_failures)
                        if current_failures
                        else "all current critical cases pass; no baseline report"
                    ),
                )
            )
        else:
            regressions: list[str] = [
                f"{case_id}:current_failure" for case_id in current_failures
            ]
            for current in cases:
                if not current.critical:
                    continue
                previous = baseline_cases[current.case_id]
                for check, previous_value in previous.checks.items():
                    if previous_value is True and current.checks.get(check) is False:
                        regressions.append(f"{current.case_id}:{check}")
                if not previous.provider_failed and current.provider_failed:
                    regressions.append(f"{current.case_id}:provider_failed")
            results.append(
                RagGateResult(
                    name="critical_no_regression",
                    status=(GateStatus.FAILED if regressions else GateStatus.PASSED),
                    detail=(
                        "critical regressions: " + ", ".join(sorted(regressions))
                        if regressions
                        else "no critical case regressed"
                    ),
                )
            )

    baseline_metrics = baseline.metrics if baseline is not None else {}
    for gate in sorted(policy.metric_gates, key=lambda item: item.metric):
        current = metrics.get(gate.metric)
        baseline_metric = baseline_metrics.get(gate.metric)
        result = _evaluate_metric_gate(gate, current, baseline_metric)
        results.append(result)
    return results


def _evaluate_metric_gate(
    gate: RagMetricGate,
    current: RagMetricResult | None,
    baseline: RagMetricResult | None,
) -> RagGateResult:
    actual = current.value if current is not None else None
    baseline_value = baseline.value if baseline is not None else None
    failures: list[str] = []
    evaluated = False
    threshold: float | None = None

    if actual is None:
        status = GateStatus.FAILED if gate.required else GateStatus.SKIPPED
        return RagGateResult(
            name=f"metric:{gate.metric}",
            metric=gate.metric,
            status=status,
            detail="metric is unavailable",
        )

    if gate.minimum is not None:
        evaluated = True
        threshold = gate.minimum
        if actual < gate.minimum:
            failures.append(f"below minimum {gate.minimum}")
    if gate.maximum is not None:
        evaluated = True
        threshold = gate.maximum
        if actual > gate.maximum:
            failures.append(f"above maximum {gate.maximum}")

    if baseline is not None:
        evaluated = True
        if baseline_value is None:
            if gate.required:
                failures.append("baseline metric is unavailable")
        elif (
            current is not None
            and current.eligible_cases < baseline.eligible_cases
            and gate.required
        ):
            failures.append(
                "eligible case count dropped from "
                f"{baseline.eligible_cases} to {current.eligible_cases}"
            )
        elif gate.direction is MetricDirection.HIGHER_IS_BETTER:
            threshold = _round(baseline_value - gate.max_regression)
            if actual < threshold:
                failures.append(
                    f"regressed more than {gate.max_regression} from baseline"
                )
        else:
            threshold = _round(baseline_value + gate.max_regression)
            if actual > threshold:
                failures.append(
                    f"regressed more than {gate.max_regression} from baseline"
                )

    status = (
        GateStatus.FAILED
        if failures
        else (GateStatus.PASSED if evaluated else GateStatus.SKIPPED)
    )
    return RagGateResult(
        name=f"metric:{gate.metric}",
        metric=gate.metric,
        status=status,
        actual=actual,
        baseline=baseline_value,
        threshold=threshold,
        detail="; ".join(failures) if failures else "gate satisfied",
    )


def _index_observations(
    observations: Sequence[RagEvalObservation],
) -> dict[str, RagEvalObservation]:
    indexed: dict[str, RagEvalObservation] = {}
    for observation in observations:
        if observation.case_id in indexed:
            raise ObservationCoverageError(
                f"duplicate observation for case {observation.case_id}"
            )
        indexed[observation.case_id] = observation
    return indexed


def _optional_equal(expected: Any, actual: Any) -> bool | None:
    return None if expected is None else expected == actual


def _mean(values: Sequence[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return _round(sum(present) / len(present)) if present else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    rank = max(math.ceil(percentile * len(values)), 1)
    return _round(sorted(values)[rank - 1])


def _round(value: float) -> float:
    return round(float(value), 12)


def _format_number(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.6f}".rstrip("0").rstrip(".")


__all__ = [
    "DatasetFingerprintMismatch",
    "GateStatus",
    "MetricDirection",
    "ObservationCoverageError",
    "RagComplexity",
    "RagEvalCase",
    "RagEvalCaseResult",
    "RagEvalDataset",
    "RagEvalGatePolicy",
    "RagEvalObservation",
    "RagEvalObservationBundle",
    "RagEvalReport",
    "RagEvalSliceResult",
    "RagEvaluationError",
    "RagExpectedBehavior",
    "RagGoldChunk",
    "RagGoldDocument",
    "RagGateResult",
    "RagHitlKind",
    "RagMetricGate",
    "RagMetricResult",
    "RagOutcome",
    "RagRetrievedChunk",
    "RagRoute",
    "dataset_fingerprint",
    "evaluate_rag",
    "load_rag_eval_dataset",
    "load_rag_eval_gates",
    "load_rag_eval_observations",
    "load_rag_eval_report",
    "render_rag_eval_json",
    "render_rag_eval_markdown",
]
