from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from backend.evaluation.rag import (  # noqa: E402
    DatasetFingerprintMismatch,
    MetricDirection,
    RagEvalGatePolicy,
    RagEvalObservationBundle,
    RagEvalReport,
    RagEvaluationError,
    evaluate_rag,
    load_rag_eval_dataset,
    load_rag_eval_gates,
    load_rag_eval_report,
    render_rag_eval_json,
    render_rag_eval_markdown,
)
from backend.evaluation.rag_adapters import (  # noqa: E402
    LiveRagEvalAdapter,
    PredictionFileAdapter,
    RagEvalExecutionError,
    artifact_tree_fingerprint,
    live_rag_profile_snapshot,
    profile_fingerprint,
    rag_source_fingerprint,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, execute, and score versioned RAG evaluations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a Dataset")
    validate.add_argument("--dataset", required=True, type=Path)

    score = subparsers.add_parser(
        "score",
        help="score a sanitized Observation file without production dependencies",
    )
    _add_common_score_arguments(score, live=False)
    score.add_argument("--observations", required=True, type=Path)

    run = subparsers.add_parser(
        "run",
        help="execute the current local RAG graph and score its Observations",
    )
    _add_common_score_arguments(run, live=True)
    run.add_argument("--observations", required=True, type=Path)
    run.add_argument("--timeout-seconds", type=_positive_finite, default=60.0)
    run.add_argument("--user-id", default="rag_eval")
    return parser


def _add_common_score_arguments(
    parser: argparse.ArgumentParser,
    *,
    live: bool,
) -> None:
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--corpus-id")
    parser.add_argument(
        "--corpus-path",
        type=Path,
        default=PROJECT_ROOT / "evals/rag/corpus",
    )
    parser.add_argument(
        "--profile-id",
        type=_identifier,
        required=live,
        default=None if live else "offline-smoke-v1",
    )
    parser.add_argument(
        "--index-id",
        type=_identifier,
        required=live,
        default=None if live else "controlled-corpus-v1",
    )
    parser.add_argument(
        "--allow-source-mismatch",
        action="store_true",
        help="allow comparison with a baseline from another RAG source fingerprint",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="return exit code 1 when a quality gate fails",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            dataset = load_rag_eval_dataset(args.dataset)
            from backend.evaluation.rag import dataset_fingerprint

            print(
                json.dumps(
                    {
                        "case_count": len(dataset.cases),
                        "dataset": dataset.name,
                        "dataset_fingerprint": dataset_fingerprint(dataset),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        dataset = load_rag_eval_dataset(args.dataset)
        gates = load_rag_eval_gates(args.gates)
        _validate_release_policy(gates)
        baseline = load_rag_eval_report(args.baseline) if args.baseline else None
        corpus_id = args.corpus_id or dataset.name
        metadata = _metadata(
            live=args.command == "run",
            corpus_id=corpus_id,
            corpus_path=args.corpus_path,
            profile_id=args.profile_id,
            index_id=args.index_id,
            source_mismatch_override=bool(
                baseline is not None and args.allow_source_mismatch
            ),
        )
        if baseline is not None and not args.allow_source_mismatch:
            _require_comparable_source(metadata, baseline)

        if args.command == "run":
            bundle = LiveRagEvalAdapter(
                timeout_seconds=args.timeout_seconds,
                user_id=args.user_id,
            ).execute(dataset)
            _write_observations(args.observations, bundle)
        else:
            bundle = PredictionFileAdapter(args.observations).execute(dataset)

        report = evaluate_rag(
            dataset,
            bundle,
            gates,
            baseline=baseline,
            metadata=metadata,
        )
        _write_text(args.report, render_rag_eval_json(report))
        if args.markdown:
            _write_text(args.markdown, render_rag_eval_markdown(report))

        print(
            json.dumps(
                {
                    "dataset": report.dataset_name,
                    "passed": report.passed,
                    "report": str(args.report),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if args.fail_on_regression and not report.passed:
            return 1
        return 0
    except (
        DatasetFingerprintMismatch,
        OSError,
        RagEvaluationError,
        RagEvalExecutionError,
        RuntimeError,
        ValidationError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"RAG evaluation error: {_safe_error_message(exc)}", file=sys.stderr)
        return 2


def _metadata(
    *,
    live: bool,
    corpus_id: str,
    corpus_path: Path,
    profile_id: str,
    index_id: str,
    source_mismatch_override: bool,
) -> dict[str, Any]:
    if live:
        from backend.env import load_env

        load_env()
        profile = live_rag_profile_snapshot(
            profile_id=profile_id,
            index_id=index_id,
        )
        provenance = "live_rag"
        adapter = "live_rag"
    else:
        profile = {
            "profile_id": profile_id,
            "index_id": index_id,
            "mode": "offline_contract_smoke",
        }
        provenance = "contract_smoke"
        adapter = "prediction_file"
    return {
        "adapter": adapter,
        "provenance": provenance,
        "corpus_id": corpus_id,
        "corpus_fingerprint": artifact_tree_fingerprint(corpus_path),
        "profile_id": profile_id,
        "index_id": index_id,
        "profile_fingerprint": profile_fingerprint(profile),
        "rag_source_fingerprint": rag_source_fingerprint(PROJECT_ROOT),
        "source_mismatch_override": source_mismatch_override,
    }


def _require_comparable_source(
    metadata: dict[str, Any],
    baseline: RagEvalReport,
) -> None:
    mismatches = [
        key
        for key in (
            "corpus_id",
            "corpus_fingerprint",
            "profile_id",
            "index_id",
            "profile_fingerprint",
            "rag_source_fingerprint",
        )
        if baseline.metadata.get(key) != metadata.get(key)
    ]
    if mismatches:
        raise DatasetFingerprintMismatch(
            "baseline source identity differs: " + ", ".join(mismatches)
        )


def _validate_release_policy(policy: RagEvalGatePolicy) -> None:
    if not policy.critical_no_regression:
        raise RagEvaluationError("release gate policy must protect critical cases")
    if policy.required_provenance is None:
        raise RagEvaluationError("release gate policy must require provenance")
    if 10 not in policy.k_values or max(policy.k_values) > 100:
        raise RagEvaluationError(
            "release gate policy must include top-10 and cannot exceed top-100"
        )

    release_k = 10
    required = {
        "case_pass_rate": (MetricDirection.HIGHER_IS_BETTER, 0.95, None, 0.0),
        "complexity_accuracy": (
            MetricDirection.HIGHER_IS_BETTER,
            0.9,
            None,
            0.02,
        ),
        f"document_recall_at_{release_k}": (
            MetricDirection.HIGHER_IS_BETTER,
            0.8,
            None,
            0.02,
        ),
        "gold_chunk_coverage": (
            MetricDirection.HIGHER_IS_BETTER,
            0.9,
            None,
            0.02,
        ),
        "hitl_accuracy": (MetricDirection.HIGHER_IS_BETTER, 0.9, None, 0.02),
        "hitl_final_outcome_accuracy": (
            MetricDirection.HIGHER_IS_BETTER,
            0.9,
            None,
            0.02,
        ),
        "hitl_resolution_success_rate": (
            MetricDirection.HIGHER_IS_BETTER,
            0.9,
            None,
            0.02,
        ),
        f"mrr_at_{release_k}": (
            MetricDirection.HIGHER_IS_BETTER,
            0.8,
            None,
            0.02,
        ),
        f"ndcg_at_{release_k}": (
            MetricDirection.HIGHER_IS_BETTER,
            0.8,
            None,
            0.02,
        ),
        "outcome_accuracy": (
            MetricDirection.HIGHER_IS_BETTER,
            0.9,
            None,
            0.02,
        ),
        "provider_failure_rate": (
            MetricDirection.LOWER_IS_BETTER,
            None,
            0.0,
            0.0,
        ),
        f"recall_at_{release_k}": (
            MetricDirection.HIGHER_IS_BETTER,
            0.8,
            None,
            0.02,
        ),
        f"rewrite_improvement_rate_at_{release_k}": (
            MetricDirection.HIGHER_IS_BETTER,
            0.5,
            None,
            0.02,
        ),
        "route_accuracy": (MetricDirection.HIGHER_IS_BETTER, 0.9, None, 0.02),
    }
    configured = {gate.metric: gate for gate in policy.metric_gates}
    missing = sorted(set(required) - set(configured))
    if missing:
        raise RagEvaluationError(
            "release gate policy is missing metrics: " + ", ".join(missing)
        )
    for metric, (direction, minimum, maximum, max_regression) in required.items():
        gate = configured[metric]
        if not gate.required or gate.direction is not direction:
            raise RagEvaluationError(f"release metric gate is weak: {metric}")
        if minimum is not None and (gate.minimum is None or gate.minimum < minimum):
            raise RagEvaluationError(f"release metric minimum is weak: {metric}")
        if maximum is not None and (gate.maximum is None or gate.maximum > maximum):
            raise RagEvaluationError(f"release metric maximum is weak: {metric}")
        if gate.max_regression > max_regression:
            raise RagEvaluationError(
                f"release metric regression tolerance is weak: {metric}"
            )


def _positive_finite(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return parsed


def _identifier(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 160:
        raise argparse.ArgumentTypeError("identifier length is invalid")
    if not candidate[0].isalnum() or any(
        not (character.isalnum() or character in "_.:-") for character in candidate
    ):
        raise argparse.ArgumentTypeError("identifier contains invalid characters")
    return candidate


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
            for error in exc.errors(include_input=False)
        )
    return str(exc)


def _write_observations(
    path: Path,
    bundle: RagEvalObservationBundle,
) -> None:
    payload = (
        json.dumps(
            bundle.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    _write_text(path, payload)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
