from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

import redis.asyncio as redis_async

from backend.core.settings import get_settings
from backend.runs.repository import RunRecord
from backend.runs.service import RunService, service
from backend.runs.state import RunStatus


T = TypeVar("T")


class RedisCancellationTransport:
    def __init__(self, redis_url: str | None = None, *, key_prefix: str | None = None):
        settings = get_settings()
        self.redis_url = redis_url or settings.storage.redis_url.get_secret_value()
        self.key_prefix = key_prefix or settings.storage.redis_key_prefix
        self.ttl = settings.runs.cancellation_ttl_seconds
        self._client = None

    @property
    def channel(self) -> str:
        return f"{self.key_prefix}:run_cancel:v1"

    def _key(self, run_id: str) -> str:
        return f"{self.key_prefix}:run_cancelled:v1:{run_id}"

    def _get_client(self):
        if self._client is None:
            self._client = redis_async.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
        return self._client

    async def request(self, run_id: str) -> None:
        client = self._get_client()
        async with client.pipeline(transaction=True) as pipeline:
            pipeline.set(self._key(run_id), "1", ex=self.ttl)
            pipeline.publish(self.channel, run_id)
            await pipeline.execute()

    async def is_requested(self, run_id: str) -> bool:
        return bool(await self._get_client().exists(self._key(run_id)))

    async def listen(
        self,
        stop_event: asyncio.Event,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        pubsub = self._get_client().pubsub()
        await pubsub.subscribe(self.channel)
        try:
            while not stop_event.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1,
                )
                if message and message.get("data"):
                    await callback(str(message["data"]))
                await asyncio.sleep(0)
        finally:
            await pubsub.unsubscribe(self.channel)
            await pubsub.aclose()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class CancellationToken:
    run_id: str
    event: asyncio.Event
    transport: RedisCancellationTransport | None = None
    _partial_chunks: list[str] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    @property
    def partial_content(self) -> str:
        return "".join(self._partial_chunks)

    def append_partial(self, content: str) -> None:
        if content:
            self._partial_chunks.append(content)

    async def checkpoint(self) -> None:
        if self.event.is_set():
            raise asyncio.CancelledError
        if self.transport is not None:
            try:
                if await self.transport.is_requested(self.run_id):
                    self.event.set()
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


@dataclass
class _Registration:
    token: CancellationToken
    task: asyncio.Task | None = None


class CancellationRegistry:
    def __init__(self, transport: RedisCancellationTransport | None = None):
        self.transport = transport
        self._registrations: dict[str, _Registration] = {}
        self._lock = asyncio.Lock()

    async def register(
        self, run_id: str, task: asyncio.Task | None = None
    ) -> CancellationToken:
        async with self._lock:
            registration = self._registrations.get(run_id)
            if registration is None:
                token = CancellationToken(
                    run_id=run_id,
                    event=asyncio.Event(),
                    transport=self.transport,
                )
                registration = _Registration(token=token, task=task)
                self._registrations[run_id] = registration
            elif task is not None:
                registration.task = task
            token = registration.token
        if self.transport is not None:
            try:
                if await self.transport.is_requested(run_id):
                    token.event.set()
            except Exception:
                pass
        return token

    async def unregister(self, run_id: str) -> None:
        async with self._lock:
            self._registrations.pop(run_id, None)

    async def cancel_local(self, run_id: str) -> bool:
        async with self._lock:
            registration = self._registrations.get(run_id)
            if registration is None:
                return False
            registration.token.event.set()
            task = registration.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=get_settings().runs.cancellation_wait_seconds,
                )
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                pass
        return True

    async def request_cancel(self, run_id: str, *, propagate: bool = True) -> bool:
        local = await self.cancel_local(run_id)
        if propagate and self.transport is not None:
            try:
                await self.transport.request(run_id)
            except Exception:
                pass
        return local

    async def listen(self, stop_event: asyncio.Event) -> None:
        if self.transport is None:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            try:
                await self.transport.listen(stop_event, self.cancel_local)
            except Exception:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except TimeoutError:
                    continue

    async def close(self) -> None:
        if self.transport is not None:
            await self.transport.close()


Runner = Callable[[CancellationToken], Awaitable[T]]


class RunExecutionManager:
    def __init__(
        self,
        run_service: RunService = service,
        registry: CancellationRegistry | None = None,
    ) -> None:
        self.service = run_service
        self.registry = registry or cancellation_registry

    def spawn(
        self,
        *,
        run: RunRecord,
        runner: Runner[str],
    ) -> asyncio.Task:
        async def execute() -> None:
            task = asyncio.current_task()
            token = await self.registry.register(run.id, task)
            try:
                await token.checkpoint()
                content = await runner(token)
                await token.checkpoint()
                self.service.complete_run(
                    run_id=run.id,
                    content=content,
                    fencing_token=run.fencing_token,
                )
            except asyncio.CancelledError:
                self.service.repository.finalize(
                    run_id=run.id,
                    target_status=RunStatus.CANCELLED,
                    content=token.partial_content or "运行已由用户取消。",
                    fencing_token=run.fencing_token,
                    error_code="RUN_CANCELLED",
                    error_detail_redacted="cancelled by user",
                    partial=True,
                )
            except Exception:
                self.service.fail_run(
                    run_id=run.id,
                    error_code="RUN_EXECUTION_FAILED",
                    message=token.partial_content or "运行失败，请稍后重试。",
                    fencing_token=run.fencing_token,
                    partial=bool(token.partial_content),
                )
            finally:
                await self.registry.unregister(run.id)

        return asyncio.create_task(execute(), name=f"run-execution:{run.id}")


default_cancellation_transport = RedisCancellationTransport()
cancellation_registry = CancellationRegistry(default_cancellation_transport)
execution_manager = RunExecutionManager(service, cancellation_registry)
