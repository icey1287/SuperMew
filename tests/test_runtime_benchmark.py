from __future__ import annotations

import asyncio

import pytest

from scripts.benchmark_runtime import (
    BenchmarkPolicy,
    build_report,
    percentile,
    run_benchmarks,
)


def test_percentile_uses_nearest_rank_and_rejects_empty_input() -> None:
    assert percentile([1.0, 4.0, 2.0, 3.0], 0.95) == 4.0
    with pytest.raises(ValueError, match="at least one"):
        percentile([], 0.95)


def test_small_non_model_runtime_profile_is_measurable() -> None:
    policy = BenchmarkPolicy(
        profile="test",
        samples={
            "cancel_local": 2,
            "event_publish": 2,
            "sse_format": 2,
            "thread_http_concurrent": 2,
            "thread_http_sequential": 2,
        },
        concurrency=2,
        budgets_ms={
            "cancel_local_p95_ms": 10_000,
            "event_publish_p95_ms": 10_000,
            "sse_format_p95_ms": 10_000,
            "thread_http_concurrent_p95_ms": 10_000,
            "thread_http_sequential_p95_ms": 10_000,
        },
    )

    metrics = asyncio.run(run_benchmarks(policy))
    report = build_report(policy, metrics)

    assert set(metrics) == set(policy.budgets_ms)
    assert report["passed"] is True
    assert report["failures"] == {}
