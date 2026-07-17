from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

import redis.asyncio as redis_async

from backend.core.errors import (
    AppError,
    ErrorCode,
    PublicError,
    public_error_from_exception,
    serialize_public_error,
)
from backend.core.settings import get_settings
from backend.runs.repository import RunRecord
from backend.runs.service import RunService, service
from backend.runs.state import RunStatus, TERMINAL_RUN_STATUSES


logger = logging.getLogger(__name__)

_CANCELLATION_REASON_PRIORITY = {
    "shutdown": 0,
    "user": 1,
    "ownership_lost": 2,
}


class CancellationTransport(Protocol):
    async def request(self, run_id: str) -> None: ...

    async def is_requested(self, run_id: str) -> bool: ...

    async def listen(
        self,
        stop_event: asyncio.Event,
        callback: Callable[[str], Awaitable[None]],
    ) -> None: ...

    async def close(self) -> None: ...


class RedisCancellationTransport:
    def __init__(self, redis_url: str | None = None, *, key_prefix: str | None = None):
        settings = get_settings()
        self.redis_url = redis_url or settings.storage.redis_url.get_secret_value()
        self.key_prefix = key_prefix or settings.storage.redis_key_prefix
        self.ttl = settings.runs.cancellation_ttl_seconds
        self._client: redis_async.Redis[str] | None = None

    @property
    def channel(self) -> str:
        return f"{self.key_prefix}:run_cancel:v1"

    def _key(self, run_id: str) -> str:
        return f"{self.key_prefix}:run_cancelled:v1:{run_id}"

    def _get_client(self) -> redis_async.Redis[str]:
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
    transport: CancellationTransport | None = None
    _partial_chunks: list[str] = field(default_factory=list)
    reason: Literal["user", "shutdown", "ownership_lost"] | None = None

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    @property
    def partial_content(self) -> str:
        return "".join(self._partial_chunks)

    def append_partial(self, content: str) -> None:
        if content:
            self._partial_chunks.append(content)

    def request(
        self,
        reason: Literal["user", "shutdown", "ownership_lost"],
    ) -> None:
        current_priority = (
            _CANCELLATION_REASON_PRIORITY[self.reason]
            if self.reason is not None
            else -1
        )
        if _CANCELLATION_REASON_PRIORITY[reason] > current_priority:
            self.reason = reason
        self.event.set()

    async def checkpoint(self) -> None:
        if self.event.is_set():
            raise asyncio.CancelledError
        if self.transport is not None:
            try:
                if await self.transport.is_requested(self.run_id):
                    self.request("user")
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


@dataclass
class _Registration:
    token: CancellationToken
    task: asyncio.Task[object] | None = None


class CancellationRegistry:
    def __init__(self, transport: CancellationTransport | None = None):
        self.transport = transport
        self._registrations: dict[str, _Registration] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        run_id: str,
        task: asyncio.Task[object] | None = None,
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
                    token.request("user")
            except Exception:
                pass
        return token

    async def unregister(self, run_id: str) -> None:
        async with self._lock:
            self._registrations.pop(run_id, None)

    async def cancel_local(
        self,
        run_id: str,
        reason: Literal["user", "shutdown", "ownership_lost"] = "user",
    ) -> bool:
        async with self._lock:
            registration = self._registrations.get(run_id)
            if registration is None:
                return False
            registration.token.request(reason)
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
        local = await self.cancel_local(run_id, reason="user")
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
                await self.transport.listen(stop_event, self._cancel_from_transport)
            except Exception:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except TimeoutError:
                    continue

    async def _cancel_from_transport(self, run_id: str) -> None:
        await self.cancel_local(run_id)

    async def close(self) -> None:
        if self.transport is not None:
            await self.transport.close()


@dataclass(frozen=True)
class RunExecutionOutcome:
    kind: Literal["completed", "waiting_input"]
    content: str = ""
    rag_trace: dict | None = None
    fencing_token: int | None = None


Runner = Callable[
    [CancellationToken],
    Awaitable[str | RunExecutionOutcome],
]


class RunExecutionManager:
    def __init__(
        self,
        run_service: RunService = service,
        registry: CancellationRegistry | None = None,
    ) -> None:
        self.service = run_service
        self.registry = registry or cancellation_registry

    async def _ignore_conflict_if_terminal(self, run_id: str, exc: AppError) -> None:
        if exc.code != ErrorCode.RUN_STATE_CONFLICT:
            raise exc
        current = await asyncio.to_thread(
            self.service.repository.get_internal,
            run_id=run_id,
        )
        if current.status not in TERMINAL_RUN_STATUSES:
            raise exc

    async def execute(
        self,
        *,
        run: RunRecord,
        runner: Runner,
    ) -> None:
        task = asyncio.current_task()
        token = await self.registry.register(run.id, task)
        try:
            await token.checkpoint()
            value = await runner(token)
            await token.checkpoint()
            outcome = (
                value
                if isinstance(value, RunExecutionOutcome)
                else RunExecutionOutcome(kind="completed", content=value)
            )
            if outcome.kind == "waiting_input":
                current = await asyncio.to_thread(
                    self.service.repository.get_internal,
                    run_id=run.id,
                )
                durable_pause = await asyncio.to_thread(
                    self.service.repository.has_durable_checkpoint,
                    run_id=run.id,
                )
                if (
                    current.status
                    not in {
                        RunStatus.WAITING_INPUT.value,
                        RunStatus.PENDING.value,
                        RunStatus.RUNNING.value,
                    }
                    or not durable_pause
                ):
                    raise RuntimeError(
                        "waiting_input outcome requires a durable checkpoint transition"
                    )
                return
            await asyncio.to_thread(
                self.service.complete_run,
                run_id=run.id,
                content=outcome.content,
                fencing_token=outcome.fencing_token or run.fencing_token,
                rag_trace=outcome.rag_trace,
            )
        except asyncio.CancelledError:
            if token.reason == "ownership_lost":
                return
            try:
                if token.reason == "user":
                    public_error = PublicError(
                        code=ErrorCode.RUN_CANCELLED,
                        message="运行已由用户取消。",
                        status_code=409,
                        retryable=False,
                        category="run",
                        stage="cancellation",
                    )
                    await asyncio.to_thread(
                        self.service.repository.finalize,
                        run_id=run.id,
                        target_status=RunStatus.CANCELLED,
                        content=token.partial_content or public_error.message,
                        fencing_token=run.fencing_token,
                        error_code=str(public_error.code),
                        error_detail_redacted=serialize_public_error(public_error),
                        partial=True,
                    )
                else:
                    public_error = PublicError(
                        code=ErrorCode.RUN_INTERRUPTED,
                        message="运行因服务重启而中断。",
                        status_code=503,
                        retryable=True,
                        category="run",
                        stage="shutdown",
                    )
                    await asyncio.to_thread(
                        self.service.fail_run,
                        run_id=run.id,
                        public_error=public_error,
                        message=token.partial_content or public_error.message,
                        fencing_token=run.fencing_token,
                        partial=bool(token.partial_content),
                    )
            except AppError as exc:
                await self._ignore_conflict_if_terminal(run.id, exc)
        except Exception as exc:
            public_error = public_error_from_exception(
                exc,
                fallback=PublicError(
                    code=ErrorCode.RUN_EXECUTION_FAILED,
                    message="运行失败，请稍后重试。",
                    status_code=500,
                    retryable=True,
                    category="run",
                    stage="execution",
                ),
            )
            if isinstance(exc, AppError):
                logger.warning(
                    "Run execution failed run_id=%s error_code=%s",
                    run.id,
                    public_error.code,
                )
            else:
                logger.exception(
                    "Run execution failed run_id=%s error_code=%s",
                    run.id,
                    public_error.code,
                )
            await asyncio.to_thread(
                self.service.fail_run,
                run_id=run.id,
                public_error=public_error,
                message=token.partial_content or public_error.message,
                fencing_token=run.fencing_token,
                partial=bool(token.partial_content),
            )
        finally:
            await self.registry.unregister(run.id)

    def spawn(
        self,
        *,
        run: RunRecord,
        runner: Runner,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self.execute(run=run, runner=runner),
            name=f"run-execution:{run.id}",
        )


default_cancellation_transport = RedisCancellationTransport()
cancellation_registry = CancellationRegistry(default_cancellation_transport)
execution_manager = RunExecutionManager(service, cancellation_registry)
