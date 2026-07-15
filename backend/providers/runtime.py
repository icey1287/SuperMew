from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from backend.core.settings import AppSettings, RerankSettings, get_settings
from backend.providers.embedding import (
    EmbeddingReadiness,
    EmbeddingRuntime,
    EmbeddingService,
)
from backend.providers.loop_bridge import ProviderLoopBridge, provider_loop_bridge
from backend.providers.rerank import (
    DisabledRerankerAdapter,
    HttpxRerankerAdapter,
    RerankerProvider,
)


@dataclass(frozen=True)
class ProviderRuntimeReadiness:
    running: bool
    bridge_thread_ident: int | None
    embedding: EmbeddingReadiness
    rerank_enabled: bool
    rerank_model: str | None


class ProviderRuntime:
    """Own the long-lived loop, local model, and pooled Provider Adapters."""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        bridge: ProviderLoopBridge | None = None,
        reranker_factory: Callable[[RerankSettings], RerankerProvider] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.bridge = bridge or provider_loop_bridge
        embedding = self.settings.embedding
        self.embedding_runtime = EmbeddingRuntime(
            model_name=embedding.model,
            model_revision=embedding.revision,
            device=embedding.device,
            provider_name=embedding.model.rsplit("/", 1)[-1] or "embedding-model",
            encoder_concurrency=embedding.max_concurrency,
            executor_workers=embedding.executor_workers,
            max_batch_size=embedding.query_max_batch_size,
            max_queue_size=embedding.query_queue_size,
            cache_size=embedding.query_cache_size,
            microbatch_window_seconds=embedding.query_microbatch_ms / 1000,
            expected_dimension=embedding.dimension,
            default_timeout_seconds=embedding.timeout_seconds,
        )
        self.embedding_service = EmbeddingService(
            runtime=self.embedding_runtime,
            bridge=self.bridge,
        )
        self._reranker_factory = reranker_factory or self._build_reranker
        self._reranker: RerankerProvider | None = None
        self._owner_lock: asyncio.Lock | None = None
        self._lifecycle_lock: asyncio.Lock | None = None
        self._lifecycle_loop: asyncio.AbstractEventLoop | None = None
        self._async_close_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        async with self._get_lifecycle_lock():
            if self._closed:
                raise RuntimeError("provider runtime is closed")
            await asyncio.to_thread(self.bridge.start)
            try:
                await self.bridge.call_async(self._start_on_owner_loop)
            except BaseException:
                await self._aclose_unlocked()
                raise

    def start_sync(self) -> None:
        if self._closed:
            raise RuntimeError("provider runtime is closed")
        if self._started and self.bridge.running:
            return
        self.bridge.start()
        try:
            self.bridge.call_sync(self._start_on_owner_loop)
        except BaseException:
            self.close_sync()
            raise

    def get_reranker_sync(self) -> RerankerProvider:
        self.start_sync()
        if self._reranker is None:
            raise RuntimeError("provider runtime did not create a reranker adapter")
        return self._reranker

    async def aclose(self) -> None:
        async with self._get_lifecycle_lock():
            if self._closed:
                return
            if self._async_close_task is None:
                self._async_close_task = asyncio.create_task(
                    self._aclose_unlocked(),
                    name="provider-runtime-close",
                )
            close_task = self._async_close_task
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await asyncio.shield(close_task)
                raise

    async def _aclose_unlocked(self) -> None:
        if self._closed:
            return
        try:
            if not self.bridge.running:
                await asyncio.to_thread(self.bridge.start)
            await self.bridge.call_async(self._close_on_owner_loop)
        finally:
            await asyncio.to_thread(self.bridge.close)
            self._closed = True
            self._started = False

    def close_sync(self) -> None:
        if self._closed:
            return
        try:
            self.bridge.start()
            self.bridge.call_sync(self._close_on_owner_loop)
        finally:
            self.bridge.close()
            self._closed = True
            self._started = False

    def readiness(self) -> ProviderRuntimeReadiness:
        reranker = self._reranker
        return ProviderRuntimeReadiness(
            running=self._started and self.bridge.running and not self._closed,
            bridge_thread_ident=self.bridge.thread_ident,
            embedding=self.embedding_runtime.readiness(),
            rerank_enabled=bool(reranker and reranker.enabled),
            rerank_model=(reranker.model or None) if reranker is not None else None,
        )

    async def _start_on_owner_loop(self) -> None:
        if self._owner_lock is None:
            self._owner_lock = asyncio.Lock()
        async with self._owner_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("provider runtime is closed")
            if self._reranker is None:
                rerank = self.settings.rerank
                self._reranker = self._reranker_factory(rerank)
            if self.settings.embedding.warmup_on_start:
                await self.embedding_runtime.warmup(
                    deadline=time.monotonic() + self.settings.embedding.timeout_seconds
                )
            self._started = True

    async def _close_on_owner_loop(self) -> None:
        errors: list[BaseException] = []
        try:
            if self._reranker is not None:
                await self._reranker.aclose()
        except BaseException as exc:
            errors.append(exc)
        finally:
            self._reranker = None

        try:
            await self.embedding_runtime.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            self._started = False

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("provider runtime close failed", errors)

    def _get_lifecycle_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lifecycle_lock is None:
            self._lifecycle_loop = loop
            self._lifecycle_lock = asyncio.Lock()
        elif self._lifecycle_loop is not loop:
            raise RuntimeError(
                "provider runtime lifecycle belongs to another event loop"
            )
        return self._lifecycle_lock

    @staticmethod
    def _build_reranker(settings: RerankSettings) -> RerankerProvider:
        if not settings.enabled:
            return DisabledRerankerAdapter()
        return HttpxRerankerAdapter(
            endpoint=settings.endpoint,
            model=settings.model,
            api_key=settings.api_key.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
            max_concurrency=settings.max_concurrency,
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_keepalive_connections,
            circuit_failure_threshold=settings.circuit_failure_threshold,
            circuit_reset_seconds=settings.circuit_reset_seconds,
        )


provider_runtime = ProviderRuntime()


__all__ = [
    "ProviderRuntime",
    "ProviderRuntimeReadiness",
    "provider_runtime",
]
