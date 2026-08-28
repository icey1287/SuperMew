from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import math
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")
AwaitableFactory = Callable[[], Awaitable[T]]
CancellationProbe = Callable[[], bool]


class ProviderLoopBridge:
    """Run synchronous Provider callers on one long-lived background loop.

    Async server paths should call providers directly. This bridge exists for
    synchronous callers that cannot own an event loop. A factory is accepted
    instead of an already-created coroutine so rejected calls never leak an
    un-awaited coroutine object.
    """

    def __init__(
        self,
        *,
        thread_name: str = "provider-loop",
        cancellation_poll_seconds: float = 0.025,
        startup_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cancellation_poll_seconds <= 0 or not math.isfinite(
            cancellation_poll_seconds
        ):
            raise ValueError("cancellation_poll_seconds must be positive and finite")
        if startup_timeout_seconds <= 0 or not math.isfinite(startup_timeout_seconds):
            raise ValueError("startup_timeout_seconds must be positive and finite")
        if shutdown_timeout_seconds <= 0 or not math.isfinite(shutdown_timeout_seconds):
            raise ValueError("shutdown_timeout_seconds must be positive and finite")

        self._thread_name = thread_name
        self._poll_seconds = float(cancellation_poll_seconds)
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._state = "new"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._submitted: set[concurrent.futures.Future[object]] = set()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == "running"

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state == "closed"

    @property
    def thread_ident(self) -> int | None:
        with self._lock:
            return self._thread.ident if self._thread is not None else None

    def start(self) -> ProviderLoopBridge:
        """Start the background loop once and wait until it can accept work."""

        with self._lock:
            if self._state == "closed":
                raise RuntimeError("provider loop bridge is closed")
            if self._state == "closing":
                raise RuntimeError("provider loop bridge is closing")
            if self._state == "new":
                self._state = "starting"
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name=self._thread_name,
                    daemon=True,
                )
                self._thread.start()
            ready = self._ready

        if not ready.wait(self._startup_timeout_seconds):
            self.close()
            raise RuntimeError("provider loop bridge did not start in time")

        with self._lock:
            if self._state != "running" or self._loop is None:
                raise RuntimeError("provider loop bridge failed to start")
        return self

    def call_sync(
        self,
        factory: AwaitableFactory[T],
        *,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> T:
        """Run an awaitable factory from synchronous code.

        Calling this method while any event loop is running in the current
        thread is rejected: blocking that thread would deadlock or stall its
        loop. Use ``call_async`` or call the async provider Interface directly.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "call_sync cannot run inside a running event loop; use call_async"
            )

        resolved_deadline = self._validate_deadline(deadline)
        future = self._submit(factory)
        try:
            return self._wait_sync(
                future,
                deadline=resolved_deadline,
                cancellation=cancellation,
            )
        except BaseException:
            if not future.done():
                future.cancel()
            raise

    async def call_async(
        self,
        factory: AwaitableFactory[T],
        *,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> T:
        """Await work owned by the bridge loop without blocking the caller loop."""

        resolved_deadline = self._validate_deadline(deadline)
        if not callable(factory):
            raise TypeError("factory must be callable")
        if not self.running:
            await asyncio.to_thread(self.start)
        future = self._submit(factory)
        wrapped = asyncio.wrap_future(future)
        try:
            while True:
                if self._is_cancelled(cancellation):
                    future.cancel()
                    raise asyncio.CancelledError("provider bridge call cancelled")

                timeout = self._next_wait_timeout(resolved_deadline, cancellation)
                if timeout is not None and timeout <= 0:
                    future.cancel()
                    raise TimeoutError("provider loop bridge deadline exceeded")

                if timeout is None:
                    return await wrapped

                done, _ = await asyncio.wait({wrapped}, timeout=timeout)
                if done:
                    return wrapped.result()
        except BaseException:
            future.cancel()
            raise

    def close(self) -> None:
        """Cancel outstanding work, stop the loop, and reject later calls."""

        with self._lock:
            if self._state == "closed":
                return
            if self._state == "new":
                self._state = "closed"
                self._ready.set()
                self._stopped.set()
                return

            self._state = "closing"
            submitted = tuple(self._submitted)
            loop = self._loop
            thread = self._thread

        for future in submitted:
            future.cancel()

        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

        if thread is not None and thread is not threading.current_thread():
            thread.join(self._shutdown_timeout_seconds)
            if thread.is_alive():
                raise RuntimeError("provider loop bridge did not stop in time")

        with self._lock:
            self._state = "closed"
            self._loop = None
            self._ready.set()
            self._stopped.set()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            closing = self._state == "closing"
            self._loop = loop
            if not closing:
                self._state = "running"
            self._ready.set()

        if closing:
            loop.call_soon(loop.stop)

        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            asyncio.set_event_loop(None)
            loop.close()
            with self._lock:
                self._loop = None
                self._state = "closed"
                self._stopped.set()

    async def _invoke_factory(self, factory: AwaitableFactory[T]) -> T:
        produced = factory()
        if not inspect.isawaitable(produced):
            raise TypeError("provider loop factory must return an awaitable")
        return await produced

    def _submit(self, factory: AwaitableFactory[T]) -> concurrent.futures.Future[T]:
        if not callable(factory):
            raise TypeError("factory must be callable")
        self.start()
        with self._lock:
            if self._state != "running" or self._loop is None:
                raise RuntimeError("provider loop bridge is not accepting calls")
            loop = self._loop
            coroutine = self._invoke_factory(factory)
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except BaseException:
                coroutine.close()
                raise
            self._submitted.add(future)

        def discard(completed: concurrent.futures.Future[object]) -> None:
            with self._lock:
                self._submitted.discard(completed)

        future.add_done_callback(discard)
        return future

    def _wait_sync(
        self,
        future: concurrent.futures.Future[T],
        *,
        deadline: float | None,
        cancellation: CancellationProbe | None,
    ) -> T:
        while True:
            if self._is_cancelled(cancellation):
                future.cancel()
                raise asyncio.CancelledError("provider bridge call cancelled")

            timeout = self._next_wait_timeout(deadline, cancellation)
            if timeout is not None and timeout <= 0:
                future.cancel()
                raise TimeoutError("provider loop bridge deadline exceeded")

            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError as exc:
                raise asyncio.CancelledError("provider bridge call cancelled") from exc

    def _next_wait_timeout(
        self,
        deadline: float | None,
        cancellation: CancellationProbe | None,
    ) -> float | None:
        if deadline is None:
            return self._poll_seconds if cancellation is not None else None
        remaining = deadline - self._clock()
        if cancellation is None:
            return remaining
        return min(remaining, self._poll_seconds)

    @staticmethod
    def _is_cancelled(cancellation: CancellationProbe | None) -> bool:
        return bool(cancellation and cancellation())

    @staticmethod
    def _validate_deadline(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        resolved = float(deadline)
        if not math.isfinite(resolved):
            raise ValueError("deadline must be a finite monotonic timestamp")
        return resolved


provider_loop_bridge = ProviderLoopBridge()


__all__ = ["ProviderLoopBridge", "provider_loop_bridge"]
