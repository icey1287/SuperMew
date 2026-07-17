"""Measure deterministic, non-model Runtime overhead against a versioned policy."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from backend.api.routes import threads as thread_routes
from backend.events.bus import PersistentEventBus
from backend.events.generated.run_event_v1 import RunEventType, RunEventV1
from backend.events.sse import format_sse_event
from backend.infra.auth import get_current_user
from backend.runs.cancellation import CancellationRegistry
from backend.threads.service import ThreadSummary


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "benchmarks" / "runtime_non_model_v1.json"
METRIC_NAMES = {
    "cancel_local_p95_ms",
    "event_publish_p95_ms",
    "sse_format_p95_ms",
    "thread_http_concurrent_p95_ms",
    "thread_http_sequential_p95_ms",
}


@dataclass(frozen=True)
class BenchmarkPolicy:
    profile: str
    samples: dict[str, int]
    concurrency: int
    budgets_ms: dict[str, float]


@dataclass(frozen=True)
class _AppendResult:
    event: RunEventV1
    outbox_id: int


class _MemoryJournal:
    def __init__(self) -> None:
        self.sequence = 0

    def append(self, *, run_id: str, event_type: RunEventType | str, **_: Any):
        self.sequence += 1
        resolved_type = RunEventType(event_type)
        return _AppendResult(
            event=RunEventV1(
                event_id=f"evt_benchmark_{self.sequence}",
                sequence=self.sequence,
                run_id=run_id,
                thread_id="thread_benchmark",
                type=resolved_type,
                timestamp=datetime.now(UTC),
                data={},
            ),
            outbox_id=self.sequence,
        )

    def mark_outbox_published(self, _: int) -> None:
        return None


class _MemoryTransport:
    async def publish(self, _: RunEventV1) -> None:
        await asyncio.sleep(0)


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def load_policy(path: Path) -> BenchmarkPolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported benchmark policy schema_version")
    samples = {str(key): int(value) for key, value in payload["samples"].items()}
    budgets = {str(key): float(value) for key, value in payload["budgets_ms"].items()}
    if set(budgets) != METRIC_NAMES:
        raise ValueError("benchmark policy metric set is incomplete")
    if any(value < 1 for value in samples.values()):
        raise ValueError("benchmark sample counts must be positive")
    concurrency = int(payload["concurrency"])
    if concurrency < 1:
        raise ValueError("benchmark concurrency must be positive")
    return BenchmarkPolicy(
        profile=str(payload["profile"]),
        samples=samples,
        concurrency=concurrency,
        budgets_ms=budgets,
    )


def _elapsed_ms(started: int) -> float:
    return (time.perf_counter_ns() - started) / 1_000_000


async def _benchmark_event_publish(samples: int) -> list[float]:
    bus = PersistentEventBus(
        event_journal=_MemoryJournal(),
        transport=_MemoryTransport(),
    )
    await bus.publish(run_id="run_benchmark", event_type=RunEventType.RUN_STARTED)
    values: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        await bus.publish(
            run_id="run_benchmark",
            event_type=RunEventType.MESSAGE_DELTA,
        )
        values.append(_elapsed_ms(started))
    return values


async def _benchmark_cancel_local(samples: int) -> list[float]:
    registry = CancellationRegistry()
    values: list[float] = []
    for index in range(samples):
        run_id = f"run_cancel_benchmark_{index}"
        token = await registry.register(run_id)
        started = time.perf_counter_ns()
        cancelled = await registry.cancel_local(run_id)
        values.append(_elapsed_ms(started))
        if not cancelled or not token.cancelled:
            raise RuntimeError("local cancellation benchmark lost its signal")
        await registry.unregister(run_id)
    return values


def _benchmark_sse_format(samples: int) -> list[float]:
    event = RunEventV1(
        event_id="evt_benchmark_sse",
        sequence=1,
        run_id="run_benchmark",
        thread_id="thread_benchmark",
        type=RunEventType.MESSAGE_DELTA,
        timestamp=datetime.now(UTC),
        data={"content": "benchmark"},
    )
    values: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        rendered = format_sse_event(event)
        values.append(_elapsed_ms(started))
        if not rendered.startswith("id: 1\n"):
            raise RuntimeError("SSE benchmark produced an invalid frame")
    return values


def _thread_benchmark_app() -> FastAPI:
    app = FastAPI()
    app.include_router(thread_routes.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="benchmark",
        role="user",
    )
    return app


async def _benchmark_thread_http(
    *,
    sequential_samples: int,
    concurrent_samples: int,
    concurrency: int,
) -> tuple[list[float], list[float]]:
    summary = ThreadSummary(
        thread_id="thread_benchmark",
        title="Runtime benchmark",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        message_count=2,
        version=2,
        thread_status="active",
        active_run_id=None,
        active_run_status=None,
    )
    app = _thread_benchmark_app()
    transport = httpx.ASGITransport(app=app)

    async def request_once(client: httpx.AsyncClient) -> float:
        started = time.perf_counter_ns()
        response = await client.get("/v1/threads")
        elapsed = _elapsed_ms(started)
        if response.status_code != 200:
            raise RuntimeError(f"Thread benchmark failed with {response.status_code}")
        return elapsed

    with patch.object(
        thread_routes.thread_service,
        "list_threads",
        return_value=[summary],
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://benchmark",
        ) as client:
            await request_once(client)
            sequential = [await request_once(client) for _ in range(sequential_samples)]
            concurrent: list[float] = []
            remaining = concurrent_samples
            while remaining > 0:
                batch = min(concurrency, remaining)
                concurrent.extend(
                    await asyncio.gather(*(request_once(client) for _ in range(batch)))
                )
                remaining -= batch
    return sequential, concurrent


async def run_benchmarks(policy: BenchmarkPolicy) -> dict[str, float]:
    event_publish = await _benchmark_event_publish(policy.samples["event_publish"])
    cancel_local = await _benchmark_cancel_local(policy.samples["cancel_local"])
    sse_format = _benchmark_sse_format(policy.samples["sse_format"])
    thread_sequential, thread_concurrent = await _benchmark_thread_http(
        sequential_samples=policy.samples["thread_http_sequential"],
        concurrent_samples=policy.samples["thread_http_concurrent"],
        concurrency=policy.concurrency,
    )
    return {
        "cancel_local_p95_ms": percentile(cancel_local, 0.95),
        "event_publish_p95_ms": percentile(event_publish, 0.95),
        "sse_format_p95_ms": percentile(sse_format, 0.95),
        "thread_http_concurrent_p95_ms": percentile(thread_concurrent, 0.95),
        "thread_http_sequential_p95_ms": percentile(thread_sequential, 0.95),
    }


def build_report(policy: BenchmarkPolicy, metrics: dict[str, float]) -> dict[str, Any]:
    failures = {
        name: {"actual_ms": metrics[name], "budget_ms": budget}
        for name, budget in policy.budgets_ms.items()
        if metrics[name] > budget
    }
    return {
        "schema_version": 1,
        "profile": policy.profile,
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime": {
            "implementation": platform.python_implementation(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "metrics_ms": metrics,
        "budgets_ms": policy.budgets_ms,
        "failures": failures,
        "passed": not failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_policy(args.policy)
    metrics = asyncio.run(run_benchmarks(policy))
    report = build_report(policy, metrics)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 1 if args.check and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
