from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import Any

import httpx
import pytest

from backend.providers.core import (
    ProviderCode,
    ProviderError,
    ProviderExecutor,
    ProviderPolicy,
)
from backend.providers.rerank import (
    DisabledRerankerAdapter,
    HttpxRerankerAdapter,
    RerankerProvider,
)


class _TrackingClient(httpx.AsyncClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


def _client_factory(
    handler,
) -> tuple[Any, list[dict[str, Any]], list[_TrackingClient]]:
    captured: list[dict[str, Any]] = []
    clients: list[_TrackingClient] = []
    mock_transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        captured.append(dict(kwargs))
        kwargs["transport"] = mock_transport
        client = _TrackingClient(**kwargs)
        clients.append(client)
        return client

    return factory, captured, clients


@pytest.mark.asyncio
async def test_http_adapter_reuses_one_client_and_disables_transport_retries():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"index": 1, "relevance_score": 0.75}]},
        )

    factory, captured, clients = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        timeout_seconds=5,
        max_connections=7,
        max_keepalive_connections=3,
        policy=ProviderPolicy(max_attempts=1),
        client_factory=factory,
    )
    try:
        first = await adapter.rerank(
            query="first",
            documents=["a", "b"],
            top_n=1,
        )
        second = await adapter.rerank(
            query="second",
            documents=["c", "d"],
            top_n=1,
        )

        assert len(captured) == 1
        assert len(clients) == 1
        transport = captured[0]["transport"]
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert transport._pool._retries == 0  # noqa: SLF001 - configuration invariant
        limits = captured[0]["limits"]
        assert limits.max_connections == 7
        assert limits.max_keepalive_connections == 3
        assert len(requests) == 2
        assert first.items[0].index == 1
        assert first.items[0].score == 0.75
        assert second.attempts == 1

        payload = json.loads(requests[0].content)
        assert payload == {
            "model": "rerank-v1",
            "query": "first",
            "documents": ["a", "b"],
            "top_n": 1,
            "return_documents": False,
        }
        assert requests[0].headers["authorization"] == "Bearer secret"
        native_timeout = requests[0].extensions["timeout"]
        assert 0 < native_timeout["read"] <= 5
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_retry_after_is_owned_by_provider_executor():
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.9}]},
        )

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        policy=ProviderPolicy(
            max_attempts=2,
            initial_backoff_seconds=0,
            max_backoff_seconds=1,
            max_retry_after_seconds=1,
        ),
        executor=ProviderExecutor(async_sleeper=sleeper),
        client_factory=factory,
    )
    try:
        result = await adapter.rerank(query="q", documents=["doc"], top_n=1)

        assert calls == 2
        assert sleeps == [0.25]
        assert result.attempts == 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"results": []},
        {"results": [{"index": 0}]},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {"results": [{"index": 2, "relevance_score": 0.5}]},
        {
            "results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 0, "relevance_score": 0.4},
            ]
        },
    ],
)
async def test_malformed_response_is_typed_and_never_retried(payload: Any):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        score = (
            payload.get("results", [{}])[0].get("relevance_score")
            if isinstance(payload, dict) and payload.get("results")
            else None
        )
        if isinstance(score, float) and math.isnan(score):
            return httpx.Response(
                200,
                content=b'{"results":[{"index":0,"relevance_score":NaN}]}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(200, json=payload)

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        policy=ProviderPolicy(
            max_attempts=3,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
        client_factory=factory,
    )
    try:
        with pytest.raises(ProviderError) as raised:
            await adapter.rerank(query="q", documents=["doc"], top_n=1)

        assert raised.value.code == ProviderCode.RERANK_INVALID_RESPONSE
        assert raised.value.retryable is False
        assert raised.value.attempts == 1
        assert calls == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrent_requests():
    active = 0
    max_active = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            two_started.set()
        try:
            await release.wait()
            return httpx.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": 0.8}]},
            )
        finally:
            active -= 1

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        timeout_seconds=10,
        max_concurrency=2,
        policy=ProviderPolicy(max_attempts=1),
        client_factory=factory,
    )
    tasks = [
        asyncio.create_task(
            adapter.rerank(query=f"q-{index}", documents=["doc"], top_n=1)
        )
        for index in range(5)
    ]
    try:
        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert max_active == 2
        release.set()
        await asyncio.gather(*tasks)
        assert max_active == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_circuit_opens_only_after_retryable_provider_failures_and_recovers():
    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    calls = 0
    healthy = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if not healthy:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        timeout_seconds=5,
        circuit_failure_threshold=2,
        circuit_reset_seconds=30,
        policy=ProviderPolicy(max_attempts=1),
        executor=ProviderExecutor(clock=clock),
        client_factory=factory,
        clock=clock,
    )
    try:
        for _ in range(2):
            with pytest.raises(ProviderError) as raised:
                await adapter.rerank(query="q", documents=["doc"], top_n=1)
            assert raised.value.code == ProviderCode.RERANK_UNAVAILABLE

        with pytest.raises(ProviderError) as opened:
            await adapter.rerank(query="q", documents=["doc"], top_n=1)
        assert opened.value.code == ProviderCode.RERANK_CIRCUIT_OPEN
        assert calls == 2

        clock.now += 31
        healthy = True
        recovered = await adapter.rerank(query="q", documents=["doc"], top_n=1)
        assert recovered.items[0].score == 0.8
        assert calls == 3
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_half_open_circuit_allows_only_one_probe():
    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    calls = 0
    half_open_started = asyncio.Event()
    release_half_open = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        half_open_started.set()
        await release_half_open.wait()
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        circuit_failure_threshold=1,
        circuit_reset_seconds=30,
        policy=ProviderPolicy(max_attempts=1),
        executor=ProviderExecutor(clock=clock),
        client_factory=factory,
        clock=clock,
    )
    probe: asyncio.Task | None = None
    try:
        with pytest.raises(ProviderError):
            await adapter.rerank(query="q", documents=["doc"], top_n=1)

        clock.now += 31
        probe = asyncio.create_task(
            adapter.rerank(query="q", documents=["doc"], top_n=1)
        )
        await asyncio.wait_for(half_open_started.wait(), timeout=1)

        with pytest.raises(ProviderError) as blocked:
            await adapter.rerank(query="q", documents=["doc"], top_n=1)
        assert blocked.value.code == ProviderCode.RERANK_CIRCUIT_OPEN
        assert calls == 2

        release_half_open.set()
        assert (await probe).items[0].score == 0.8
    finally:
        release_half_open.set()
        if probe is not None:
            await asyncio.gather(probe, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_stale_success_cannot_clear_a_newer_half_open_probe():
    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    stale_started = asyncio.Event()
    release_stale = asyncio.Event()
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        requests.append(query)
        if query == "stale":
            stale_started.set()
            await release_stale.wait()
        elif query == "fail":
            return httpx.Response(503)
        elif query == "probe":
            probe_started.set()
            await release_probe.wait()
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        timeout_seconds=1000,
        max_concurrency=3,
        circuit_failure_threshold=1,
        circuit_reset_seconds=30,
        policy=ProviderPolicy(max_attempts=1),
        executor=ProviderExecutor(clock=clock),
        client_factory=factory,
        clock=clock,
    )
    stale: asyncio.Task | None = None
    probe: asyncio.Task | None = None
    try:
        stale = asyncio.create_task(
            adapter.rerank(query="stale", documents=["doc"], top_n=1)
        )
        await asyncio.wait_for(stale_started.wait(), timeout=1)

        with pytest.raises(ProviderError):
            await adapter.rerank(query="fail", documents=["doc"], top_n=1)

        clock.now += 31
        probe = asyncio.create_task(
            adapter.rerank(query="probe", documents=["doc"], top_n=1)
        )
        await asyncio.wait_for(probe_started.wait(), timeout=1)

        release_stale.set()
        assert (await stale).items[0].score == 0.8

        with pytest.raises(ProviderError) as blocked:
            await adapter.rerank(query="new", documents=["doc"], top_n=1)
        assert blocked.value.code == ProviderCode.RERANK_CIRCUIT_OPEN
        assert "new" not in requests

        release_probe.set()
        assert (await probe).items[0].score == 0.8
    finally:
        release_stale.set()
        release_probe.set()
        await asyncio.gather(
            *(task for task in (stale, probe) if task is not None),
            return_exceptions=True,
        )
        await adapter.aclose()


@pytest.mark.asyncio
async def test_cancelled_half_open_bookkeeping_cannot_stick_circuit_open():
    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        requests.append(query)
        if query == "fail":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        timeout_seconds=1000,
        circuit_failure_threshold=1,
        circuit_reset_seconds=30,
        policy=ProviderPolicy(max_attempts=1),
        executor=ProviderExecutor(clock=clock),
        client_factory=factory,
        clock=clock,
    )
    original_record_success = adapter._record_circuit_success  # noqa: SLF001

    async def delayed_record_success(permit) -> None:
        transition_started.set()
        await release_transition.wait()
        await original_record_success(permit)

    adapter._record_circuit_success = delayed_record_success  # noqa: SLF001
    probe: asyncio.Task | None = None
    try:
        with pytest.raises(ProviderError):
            await adapter.rerank(query="fail", documents=["doc"], top_n=1)

        clock.now += 31
        probe = asyncio.create_task(
            adapter.rerank(query="probe", documents=["doc"], top_n=1)
        )
        await asyncio.wait_for(transition_started.wait(), timeout=1)

        probe.cancel()
        release_transition.set()
        with pytest.raises(asyncio.CancelledError):
            await probe

        recovered = await adapter.rerank(
            query="next",
            documents=["doc"],
            top_n=1,
        )
        assert recovered.items[0].score == 0.8
        assert requests == ["fail", "probe", "next"]
    finally:
        release_transition.set()
        if probe is not None:
            await asyncio.gather(probe, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_authentication_failures_do_not_open_circuit():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        circuit_failure_threshold=1,
        policy=ProviderPolicy(max_attempts=1),
        client_factory=factory,
    )
    try:
        for _ in range(2):
            with pytest.raises(ProviderError) as raised:
                await adapter.rerank(query="q", documents=["doc"], top_n=1)
            assert raised.value.code == ProviderCode.PROVIDER_AUTHENTICATION_FAILED

        result = await adapter.rerank(query="q", documents=["doc"], top_n=1)
        assert result.items[0].index == 0
        assert calls == 3
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_cancellation_wins_over_an_open_circuit_fallback():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        circuit_failure_threshold=1,
        policy=ProviderPolicy(max_attempts=1),
        client_factory=factory,
    )
    try:
        with pytest.raises(ProviderError):
            await adapter.rerank(query="q", documents=["doc"], top_n=1)

        with pytest.raises(asyncio.CancelledError):
            await adapter.rerank(
                query="q",
                documents=["doc"],
                top_n=1,
                cancellation=lambda: True,
            )
        assert calls == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_cancellation_stops_in_flight_http_request():
    started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    cancellation_requested = False

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        raise AssertionError("unreachable")

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        timeout_seconds=10,
        policy=ProviderPolicy(max_attempts=1, cancellation_poll_seconds=0.001),
        client_factory=factory,
    )
    task = asyncio.create_task(
        adapter.rerank(
            query="q",
            documents=["doc"],
            top_n=1,
            cancellation=lambda: cancellation_requested,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        cancellation_requested = True
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_disabled_adapter_and_close_are_idempotent():
    disabled = DisabledRerankerAdapter()
    assert isinstance(disabled, RerankerProvider)
    assert await disabled.rerank(query="q", documents=["doc"], top_n=1) == (
        await disabled.rerank(query="q", documents=["doc"], top_n=1)
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, clients = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        client_factory=factory,
    )
    await adapter.aclose()
    await adapter.aclose()
    assert clients[0].close_calls == 1


@pytest.mark.asyncio
async def test_http_adapter_rejects_cross_loop_use_and_close():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    factory, _, _ = _client_factory(handler)
    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        policy=ProviderPolicy(max_attempts=1),
        client_factory=factory,
    )
    failures: list[BaseException] = []

    def use_from_another_loop() -> None:
        try:
            asyncio.run(adapter.rerank(query="q", documents=["doc"], top_n=1))
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=use_from_another_loop)
    thread.start()
    thread.join(1)
    try:
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert "non-owner" in str(failures[0])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_cancelled_close_caller_still_closes_shared_http_client():
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseClient(_TrackingClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            close_started.set()
            await release_close.wait()
            await httpx.AsyncClient.aclose(self)

    client: BlockingCloseClient | None = None

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        nonlocal client
        client = BlockingCloseClient(**kwargs)
        return client

    adapter = HttpxRerankerAdapter(
        endpoint="https://rerank.example.test/v1/rerank",
        model="rerank-v1",
        api_key="secret",
        policy=ProviderPolicy(max_attempts=1),
        client_factory=factory,
    )
    close = asyncio.create_task(adapter.aclose())
    await asyncio.wait_for(close_started.wait(), timeout=1)
    close.cancel()
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await close
    await adapter.aclose()

    assert client is not None
    assert client.close_calls == 1
    assert client.is_closed is True
