from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.evaluation.rag_adapters import PredictionFileAdapter
from scripts.evaluate_rag import main


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/rag/rag_smoke_v1.json"
OBSERVATIONS = ROOT / "evals/rag/offline_smoke_observations_v1.json"
GATES = ROOT / "evals/rag/gates_v1.json"
LIVE_GATES = ROOT / "evals/rag/live_gates_v1.json"
BASELINE = ROOT / "evals/rag/baseline_v1.json"


def test_cli_scores_offline_baseline_and_writes_sanitized_reports(tmp_path):
    report = tmp_path / "report.json"
    markdown = tmp_path / "report.md"

    exit_code = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(OBSERVATIONS),
            "--gates",
            str(GATES),
            "--baseline",
            str(BASELINE),
            "--report",
            str(report),
            "--markdown",
            str(markdown),
            "--fail-on-regression",
        ]
    )

    assert exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["metadata"]["adapter"] == "prediction_file"
    serialized = report.read_text(encoding="utf-8")
    assert "Bearer " not in serialized
    assert "private evidence" not in serialized
    assert "https://" not in serialized
    assert "# RAG Evaluation: PASS" in markdown.read_text(encoding="utf-8")


def test_cli_returns_one_for_a_quality_regression(tmp_path):
    observations = json.loads(OBSERVATIONS.read_text(encoding="utf-8"))
    observations["observations"][0]["route"] = "provider_failed"
    observations_path = tmp_path / "regressed.json"
    observations_path.write_text(
        json.dumps(observations, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(observations_path),
            "--gates",
            str(GATES),
            "--baseline",
            str(BASELINE),
            "--report",
            str(tmp_path / "report.json"),
            "--fail-on-regression",
        ]
    )

    assert exit_code == 1


def test_cli_returns_two_for_source_fingerprint_mismatch(tmp_path):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    baseline["metadata"]["rag_source_fingerprint"] = "0" * 64
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    exit_code = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(OBSERVATIONS),
            "--gates",
            str(GATES),
            "--baseline",
            str(baseline_path),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert exit_code == 2

    report = tmp_path / "override-report.json"
    override_exit = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(OBSERVATIONS),
            "--gates",
            str(GATES),
            "--baseline",
            str(baseline_path),
            "--report",
            str(report),
            "--allow-source-mismatch",
        ]
    )

    assert override_exit == 0
    assert (
        json.loads(report.read_text(encoding="utf-8"))["metadata"][
            "source_mismatch_override"
        ]
        is True
    )


def test_cli_returns_two_for_invalid_dataset(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"schema_version": 1, "name": "bad", "cases": []}')

    exit_code = main(["validate", "--dataset", str(dataset)])

    assert exit_code == 2


def test_cli_rejects_an_empty_release_gate_policy(tmp_path):
    gates = tmp_path / "gates.json"
    gates.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "k_values": [10],
                "critical_no_regression": False,
                "required_provenance": None,
                "metric_gates": [],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(OBSERVATIONS),
            "--gates",
            str(gates),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert exit_code == 2


def test_cli_release_policy_cannot_replace_top_ten_with_an_unbounded_k(tmp_path):
    gates = json.loads(GATES.read_text(encoding="utf-8"))
    gates["k_values"] = [1000]
    gates_path = tmp_path / "gates.json"
    gates_path.write_text(json.dumps(gates), encoding="utf-8")

    exit_code = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(OBSERVATIONS),
            "--gates",
            str(gates_path),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert exit_code == 2


def test_prediction_file_cannot_satisfy_live_provenance_gate(tmp_path):
    exit_code = main(
        [
            "score",
            "--dataset",
            str(DATASET),
            "--observations",
            str(OBSERVATIONS),
            "--gates",
            str(LIVE_GATES),
            "--report",
            str(tmp_path / "report.json"),
            "--fail-on-regression",
        ]
    )

    assert exit_code == 1


def test_cli_rejects_non_finite_live_timeout_with_exit_two(tmp_path):
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "run",
                "--dataset",
                str(DATASET),
                "--observations",
                str(tmp_path / "observations.json"),
                "--gates",
                str(LIVE_GATES),
                "--report",
                str(tmp_path / "report.json"),
                "--profile-id",
                "local-profile",
                "--index-id",
                "local-index",
                "--timeout-seconds",
                "inf",
            ]
        )

    assert raised.value.code == 2


def test_live_cli_pins_adapter_to_requested_index(monkeypatch, tmp_path):
    captured = {}

    class FakeLiveAdapter:
        def __init__(
            self,
            *,
            timeout_seconds,
            user_id,
            expected_index_id,
        ):
            captured.update(
                timeout_seconds=timeout_seconds,
                user_id=user_id,
                expected_index_id=expected_index_id,
            )

        def execute(self, dataset):
            return PredictionFileAdapter(OBSERVATIONS).execute(dataset)

    monkeypatch.setattr(
        "scripts.evaluate_rag.LiveRagEvalAdapter",
        FakeLiveAdapter,
    )
    monkeypatch.setattr(
        "scripts.evaluate_rag._metadata",
        lambda **_kwargs: {
            "adapter": "live_rag",
            "provenance": "live_rag",
            "corpus_id": "rag_smoke_v1",
            "corpus_fingerprint": "a" * 64,
            "profile_id": "release-profile",
            "index_id": "catalog-index-v2",
            "profile_fingerprint": "b" * 64,
            "rag_source_fingerprint": "c" * 64,
            "source_mismatch_override": False,
        },
    )

    exit_code = main(
        [
            "run",
            "--dataset",
            str(DATASET),
            "--observations",
            str(tmp_path / "observations.json"),
            "--gates",
            str(LIVE_GATES),
            "--report",
            str(tmp_path / "report.json"),
            "--profile-id",
            "release-profile",
            "--index-id",
            "catalog-index-v2",
            "--timeout-seconds",
            "45",
            "--user-id",
            "release-eval",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "timeout_seconds": 45.0,
        "user_id": "release-eval",
        "expected_index_id": "catalog-index-v2",
    }
