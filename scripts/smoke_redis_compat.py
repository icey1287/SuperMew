"""Exercise Redis Streams, cancellation, and rate limits against a real server."""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as redis_async
from redis.exceptions import AuthenticationError

from backend.events.generated.run_event_v1 import RunEventType, RunEventV1
from backend.events.redis_transport import RedisEventTransport
from backend.rate_limits.adapters import RedisRateLimitAdapter
from backend.rate_limits.contracts import (
    RateLimitCheck,
    RateLimitPolicy,
    RateLimitUnavailable,
)
from backend.rate_limits.limiter import RateLimiter
from backend.rate_limits.policy import RoutePolicyMatcher
from backend.runs.cancellation import RedisCancellationTransport


async def _wait_for_subscriber(
    client: redis_async.Redis,
    *,
    channel: str,
    listener: asyncio.Task[None],
    timeout: float = 3.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if listener.done():
            await listener
            raise AssertionError(
                "Redis cancellation listener exited before subscribing"
            )

        subscribers = await client.pubsub_numsub(channel)
        if subscribers and subscribers[0][1] >= 1:
            return

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(
                f"Redis cancellation listener did not subscribe to {channel!r}"
            )
        await asyncio.sleep(min(0.02, remaining))


async def _wait_for_fresh_rate_limit_window(
    client: redis_async.Redis,
    *,
    timeout: float = 3.0,
) -> None:
    """Start the shared-count check near the beginning of a Redis second."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        _seconds, microseconds = await client.time()
        if int(microseconds) <= 100_000:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Redis rate limit window did not advance")
        await asyncio.sleep(min(0.01, remaining))


async def _wait_for_rate_limit_reset(
    client: redis_async.Redis,
    *,
    reset_at: int,
    timeout: float = 3.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        seconds, _microseconds = await client.time()
        if int(seconds) >= reset_at:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Redis rate limit window did not reset")
        await asyncio.sleep(min(0.01, remaining))


async def _exercise_rate_limits(
    *,
    redis_url: str,
    cleanup: redis_async.Redis,
    key_prefix: str,
    identity_hmac_key: bytes,
) -> dict[str, object]:
    policy = RateLimitPolicy(
        id="compat-shared",
        limit=2,
        window_seconds=1,
    )
    matcher = RoutePolicyMatcher(rules=(), fallback=policy)
    first_limiter = RateLimiter(
        RedisRateLimitAdapter(redis_url),
        identity_hmac_key=identity_hmac_key,
        matcher=matcher,
        key_prefix=key_prefix,
    )
    second_limiter = RateLimiter(
        RedisRateLimitAdapter(redis_url),
        identity_hmac_key=identity_hmac_key,
        matcher=matcher,
        key_prefix=key_prefix,
    )
    raw_token = f"opaque-token-{uuid4().hex}"
    shared_identity = f"bearer:{raw_token}"
    isolated_token = f"isolated-token-{uuid4().hex}"
    isolated_identity = f"bearer:{isolated_token}"
    shared_check = RateLimitCheck(
        method="POST",
        path="/v1/threads/thread-redis-smoke/runs",
        client_identity=shared_identity,
    )
    isolated_check = RateLimitCheck(
        method="POST",
        path="/v1/threads/thread-redis-smoke/runs",
        client_identity=isolated_identity,
    )

    try:
        await _wait_for_fresh_rate_limit_window(cleanup)
        first = await first_limiter.check(shared_check)
        second = await second_limiter.check(shared_check)
        blocked = await first_limiter.check(shared_check)
        isolated = await second_limiter.check(isolated_check)

        if not first.allowed or first.remaining != 1:
            raise AssertionError(first)
        if not second.allowed or second.remaining != 0:
            raise AssertionError(
                "independent Redis rate limit instances did not share a count"
            )
        if blocked.allowed or blocked.remaining != 0 or blocked.retry_after < 1:
            raise AssertionError(blocked)
        if not isolated.allowed or isolated.remaining != 1:
            raise AssertionError("rate limit identities were not isolated")

        keys = [key async for key in cleanup.scan_iter(match=f"{key_prefix}:*")]
        if len(keys) != 2:
            raise AssertionError(f"expected two rate limit keys, found {keys!r}")
        raw_markers = (
            shared_identity,
            raw_token,
            isolated_identity,
            isolated_token,
        )
        if any(marker in key for marker in raw_markers for key in keys):
            raise AssertionError("Redis rate limit key exposed a raw identity")

        await _wait_for_rate_limit_reset(cleanup, reset_at=blocked.reset)
        after_reset = await second_limiter.check(shared_check)
        if not after_reset.allowed or after_reset.remaining != 1:
            raise AssertionError("fixed rate limit window did not reset")

        return {
            "identity_isolation": True,
            "key_redaction": True,
            "shared_count": True,
            "window_reset": True,
        }
    finally:
        await asyncio.gather(first_limiter.close(), second_limiter.close())


async def _exercise_rate_limit_fail_closed() -> str:
    limiter = RateLimiter(
        RedisRateLimitAdapter("redis://127.0.0.1:0/0"),
        identity_hmac_key=secrets.token_bytes(32),
        key_prefix=f"supermew:compat:unavailable:{uuid4().hex}",
    )
    try:
        try:
            await limiter.check(
                RateLimitCheck(
                    method="GET",
                    path="/v1/threads",
                    client_identity="unavailable-smoke",
                )
            )
        except RateLimitUnavailable as exc:
            if exc.adapter != "redis" or not exc.retryable:
                raise AssertionError(exc.safe_details) from exc
            return "typed-fail-closed"
        raise AssertionError("unavailable Redis rate limit storage failed open")
    finally:
        await limiter.close()


async def main() -> None:
    redis_url = os.environ["REDIS_URL"]
    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("REDIS_PORT", "6379"))
    prefix = f"supermew:compat:{uuid4().hex}"
    rate_limit_prefix = f"supermew:compat:rate:{uuid4().hex}"
    rate_limit_hmac_key = secrets.token_bytes(32)
    run_id = f"run_{uuid4().hex}"

    unauthenticated = redis_async.Redis(
        host=host,
        port=port,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        try:
            await unauthenticated.ping()
        except AuthenticationError:
            pass
        else:
            raise AssertionError("Redis accepted an unauthenticated connection")
    finally:
        await unauthenticated.aclose()

    event_transport = RedisEventTransport(redis_url, key_prefix=prefix)
    cancellation_listener = RedisCancellationTransport(redis_url, key_prefix=prefix)
    cancellation_requester = RedisCancellationTransport(redis_url, key_prefix=prefix)
    cleanup = redis_async.Redis.from_url(redis_url, decode_responses=True)
    stop_event = asyncio.Event()
    callback_received = asyncio.Event()

    async def on_cancel(received_run_id: str) -> None:
        if received_run_id == run_id:
            callback_received.set()
            stop_event.set()

    listener = asyncio.create_task(cancellation_listener.listen(stop_event, on_cancel))
    try:
        first = RunEventV1(
            event_id=f"evt_{uuid4().hex}",
            sequence=1,
            run_id=run_id,
            thread_id="thread-redis-smoke",
            type=RunEventType.RUN_CREATED,
            timestamp=datetime.now(timezone.utc),
            data={"status": "queued"},
        )
        second = first.model_copy(
            update={
                "event_id": f"evt_{uuid4().hex}",
                "sequence": 2,
                "type": RunEventType.RUN_STARTED,
                "data": {},
            }
        )
        await event_transport.publish(first)
        await event_transport.publish(first)
        await event_transport.publish(second)

        replay = await event_transport.wait_after(
            run_id=run_id,
            after=0,
            block_ms=50,
        )
        tail = await event_transport.wait_after(
            run_id=run_id,
            after=1,
            block_ms=50,
        )
        if [event.sequence for event in replay] != [1, 2]:
            raise AssertionError(replay)
        if [event.sequence for event in tail] != [2]:
            raise AssertionError(tail)

        await _wait_for_subscriber(
            cleanup,
            channel=cancellation_listener.channel,
            listener=listener,
        )
        await cancellation_requester.request(run_id)
        if not await cancellation_requester.is_requested(run_id):
            raise AssertionError("cancellation key was not persisted")
        await asyncio.wait_for(callback_received.wait(), timeout=3)

        rate_limits = await _exercise_rate_limits(
            redis_url=redis_url,
            cleanup=cleanup,
            key_prefix=rate_limit_prefix,
            identity_hmac_key=rate_limit_hmac_key,
        )
        unavailable_rate_limit = await _exercise_rate_limit_fail_closed()

        print(
            {
                "authentication": "required",
                "cancellation_callback": True,
                "event_sequences": [event.sequence for event in replay],
                "rate_limit": rate_limits,
                "rate_limit_unavailable": unavailable_rate_limit,
            }
        )
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(listener, timeout=2)
        except TimeoutError:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        await event_transport.close()
        await cancellation_listener.close()
        await cancellation_requester.close()
        keys = [
            key
            for match in (f"{prefix}:*", f"{rate_limit_prefix}:*")
            async for key in cleanup.scan_iter(match=match)
        ]
        if keys:
            await cleanup.delete(*keys)
        await cleanup.aclose()


if __name__ == "__main__":
    asyncio.run(main())
