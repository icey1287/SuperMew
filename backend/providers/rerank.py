from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any, Protocol, runtime_checkable

import httpx

from backend.providers.core import (
    ProviderCallContext,
    ProviderCode,
    ProviderError,
    ProviderExecutor,
    ProviderOperation,
    ProviderPolicy,
    provider_executor,
)


CancellationProbe = Callable[[], bool]
AsyncClientFactory = Callable[..., httpx.AsyncClient]


@dataclass(frozen=True, slots=True)
class RerankItem:
    """One validated provider score, referring to the submitted document index."""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    """A provider-independent rerank result."""

    items: tuple[RerankItem, ...]
    attempts: int


@dataclass(frozen=True, slots=True)
class _CircuitPermit:
    generation: int
    half_open: bool


@runtime_checkable
class RerankerProvider(Protocol):
    """Async Interface implemented by concrete reranking Adapters."""

    @property
    def enabled(self) -> bool: ...

    @property
    def model(self) -> str: ...

    @property
    def timeout_seconds(self) -> float: ...

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> RerankResult: ...

    async def aclose(self) -> None: ...


class DisabledRerankerAdapter:
    """Explicit Adapter for installations without a configured reranker."""

    enabled = False
    model = ""
    timeout_seconds = 0.0

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> RerankResult:
        del query, documents, top_n, deadline
        if cancellation is not None and cancellation():
            raise asyncio.CancelledError("rerank call cancelled")
        return RerankResult(items=(), attempts=0)

    async def aclose(self) -> None:
        return None


class HttpxRerankerAdapter:
    """Pooled async HTTP Adapter with one retry layer and a small circuit breaker."""

    enabled = True

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 5.0,
        max_concurrency: int = 4,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 30.0,
        policy: ProviderPolicy | None = None,
        executor: ProviderExecutor = provider_executor,
        client_factory: AsyncClientFactory = httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        endpoint = str(endpoint or "").strip()
        model = str(model or "").strip()
        api_key = str(api_key or "").strip()
        if not endpoint:
            raise ValueError("rerank endpoint is required")
        if not model:
            raise ValueError("rerank model is required")
        if not api_key:
            raise ValueError("rerank api key is required")
        self._validate_positive_finite("timeout_seconds", timeout_seconds)
        self._validate_positive_int("max_concurrency", max_concurrency)
        self._validate_positive_int("max_connections", max_connections)
        if (
            isinstance(max_keepalive_connections, bool)
            or not isinstance(max_keepalive_connections, int)
            or max_keepalive_connections < 0
        ):
            raise ValueError("max_keepalive_connections must be a non-negative int")
        if max_keepalive_connections > max_connections:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")
        self._validate_positive_int(
            "circuit_failure_threshold", circuit_failure_threshold
        )
        self._validate_positive_finite("circuit_reset_seconds", circuit_reset_seconds)
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        try:
            self._owner_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "HttpxRerankerAdapter must be created on its owner event loop"
            ) from exc

        self.endpoint = endpoint
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self._executor = executor
        self._policy = policy or ProviderPolicy(max_attempts=2)
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_reset_seconds = float(circuit_reset_seconds)
        self._circuit_lock = asyncio.Lock()
        self._circuit_failures = 0
        self._circuit_open_until: float | None = None
        self._half_open_in_flight = False
        self._circuit_generation = 0
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        transport = httpx.AsyncHTTPTransport(retries=0, limits=limits)
        self._client = client_factory(
            transport=transport,
            limits=limits,
            timeout=None,
            follow_redirects=False,
        )
        if not isinstance(self._client, httpx.AsyncClient):
            raise TypeError("client_factory must return httpx.AsyncClient")

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> RerankResult:
        self._assert_owner_loop()
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
            raise TypeError("documents must be a sequence of strings")
        if any(not isinstance(document, str) for document in documents):
            raise TypeError("documents must contain only strings")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n <= 0:
            raise ValueError("top_n must be a positive int")
        if cancellation is not None and cancellation():
            raise asyncio.CancelledError("rerank call cancelled")
        if not documents:
            return RerankResult(items=(), attempts=0)

        stage_deadline = self._stage_deadline(deadline)
        context = ProviderCallContext(
            provider=self.model,
            operation=ProviderOperation.RERANK,
            deadline=stage_deadline,
            cancellation=cancellation,
        )
        self._checkpoint_context(context)
        if self._closed or self._closing:
            raise ProviderError(
                ProviderCode.RERANK_UNAVAILABLE,
                context=context,
            )

        circuit_permit = await self._before_circuit_call(context)
        attempts = 0
        bounded_top_n = min(top_n, len(documents))

        async def attempt() -> tuple[RerankItem, ...]:
            nonlocal attempts
            attempts += 1
            async with self._semaphore:
                remaining = stage_deadline - self._clock()
                if remaining <= 0:
                    raise ProviderError.deadline_exceeded(context)
                response = await self._client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": list(documents),
                        "top_n": bounded_top_n,
                        "return_documents": False,
                    },
                    timeout=httpx.Timeout(remaining),
                )
                response.raise_for_status()
                return self._parse_response(
                    response,
                    document_count=len(documents),
                    top_n=bounded_top_n,
                    context=context,
                )

        try:
            items = await self._executor.acall(
                attempt,
                context=context,
                policy=self._policy,
            )
        except asyncio.CancelledError:
            transition = self._schedule_circuit_transition(
                self._abort_circuit_trial(circuit_permit)
            )
            await self._await_circuit_transition(transition)
            raise
        except ProviderError as exc:
            transition = self._schedule_circuit_transition(
                self._record_circuit_failure(exc, circuit_permit)
            )
            await self._await_circuit_transition(transition)
            raise
        except BaseException:
            transition = self._schedule_circuit_transition(
                self._abort_circuit_trial(circuit_permit)
            )
            await self._await_circuit_transition(transition)
            raise

        transition = self._schedule_circuit_transition(
            self._record_circuit_success(circuit_permit)
        )
        await self._await_circuit_transition(transition)
        return RerankResult(items=items, attempts=attempts)

    async def aclose(self) -> None:
        self._assert_owner_loop()
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(),
                name="rerank-adapter-close",
            )
        close_task = self._close_task
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await asyncio.shield(close_task)
            raise
        finally:
            if (
                close_task.done()
                and not self._closed
                and self._close_task is close_task
            ):
                self._close_task = None

    async def _close_impl(self) -> None:
        self._closing = True
        try:
            await self._client.aclose()
        except BaseException:
            self._closing = False
            raise
        self._closed = True
        self._closing = False

    def _stage_deadline(self, deadline: float | None) -> float:
        configured = self._clock() + self.timeout_seconds
        if deadline is None:
            return configured
        resolved = float(deadline)
        if not math.isfinite(resolved):
            raise ValueError("deadline must be a finite monotonic timestamp")
        return min(resolved, configured)

    def _assert_owner_loop(self) -> None:
        if asyncio.get_running_loop() is not self._owner_loop:
            raise RuntimeError("rerank adapter used from a non-owner event loop")

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        document_count: int,
        top_n: int,
        context: ProviderCallContext,
    ) -> tuple[RerankItem, ...]:
        try:
            body: Any = response.json()
        except Exception as exc:
            raise self._invalid_response(context) from exc
        if not isinstance(body, dict) or "results" not in body:
            raise self._invalid_response(context)
        results = body["results"]
        if not isinstance(results, list) or not results or len(results) > top_n:
            raise self._invalid_response(context)

        parsed: list[RerankItem] = []
        seen_indices: set[int] = set()
        for item in results:
            if not isinstance(item, dict):
                raise self._invalid_response(context)
            index = item.get("index")
            score = item.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < document_count
                or index in seen_indices
            ):
                raise self._invalid_response(context)
            if isinstance(score, bool) or not isinstance(score, Real):
                raise self._invalid_response(context)
            parsed_score = float(score)
            if not math.isfinite(parsed_score):
                raise self._invalid_response(context)
            seen_indices.add(index)
            parsed.append(RerankItem(index=index, score=parsed_score))
        return tuple(parsed)

    @staticmethod
    def _invalid_response(context: ProviderCallContext) -> ProviderError:
        return ProviderError(ProviderCode.RERANK_INVALID_RESPONSE, context=context)

    async def _before_circuit_call(
        self,
        context: ProviderCallContext,
    ) -> _CircuitPermit:
        async with self._circuit_lock:
            self._checkpoint_context(context)
            if self._half_open_in_flight:
                raise ProviderError(
                    ProviderCode.RERANK_CIRCUIT_OPEN,
                    context=context,
                )
            if self._circuit_open_until is None:
                return _CircuitPermit(self._circuit_generation, False)
            now = self._clock()
            if now < self._circuit_open_until:
                raise ProviderError(
                    ProviderCode.RERANK_CIRCUIT_OPEN,
                    context=context,
                )
            self._half_open_in_flight = True
            self._circuit_open_until = None
            self._circuit_generation += 1
            return _CircuitPermit(self._circuit_generation, True)

    def _checkpoint_context(self, context: ProviderCallContext) -> None:
        if context.cancellation is not None and context.cancellation():
            raise asyncio.CancelledError("rerank call cancelled")
        if context.deadline is not None and self._clock() >= context.deadline:
            raise ProviderError.deadline_exceeded(context)

    @staticmethod
    def _schedule_circuit_transition(
        transition: Awaitable[None],
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            transition,
            name="rerank-circuit-transition",
        )

    @staticmethod
    async def _await_circuit_transition(transition: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(transition)
        except asyncio.CancelledError:
            await asyncio.shield(transition)
            raise

    async def _record_circuit_success(self, permit: _CircuitPermit) -> None:
        async with self._circuit_lock:
            if permit.generation != self._circuit_generation:
                return
            self._circuit_failures = 0
            self._circuit_open_until = None
            self._half_open_in_flight = False
            if permit.half_open:
                self._circuit_generation += 1

    async def _record_circuit_failure(
        self,
        error: ProviderError,
        permit: _CircuitPermit,
    ) -> None:
        code = getattr(error.code, "value", str(error.code))
        async with self._circuit_lock:
            if permit.generation != self._circuit_generation:
                return
            self._half_open_in_flight = False
            if error.retryable and code != ProviderCode.RERANK_CIRCUIT_OPEN.value:
                self._circuit_failures += 1
                if self._circuit_failures >= self._circuit_failure_threshold:
                    self._circuit_open_until = (
                        self._clock() + self._circuit_reset_seconds
                    )
                    self._circuit_generation += 1
                return
            self._circuit_failures = 0
            self._circuit_open_until = None
            if permit.half_open:
                self._circuit_generation += 1

    async def _abort_circuit_trial(self, permit: _CircuitPermit) -> None:
        if not permit.half_open:
            return
        async with self._circuit_lock:
            if permit.generation != self._circuit_generation:
                return
            self._half_open_in_flight = False
            self._circuit_open_until = self._clock()
            self._circuit_generation += 1

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive int")

    @staticmethod
    def _validate_positive_finite(name: str, value: float) -> None:
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"{name} must be positive and finite")


# Short alias retained for callers that use the operation name rather than the role name.
RerankProvider = RerankerProvider


__all__ = [
    "DisabledRerankerAdapter",
    "HttpxRerankerAdapter",
    "RerankItem",
    "RerankProvider",
    "RerankResult",
    "RerankerProvider",
]
