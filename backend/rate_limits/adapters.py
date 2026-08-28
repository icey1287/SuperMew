"""Atomic storage Adapters for the inbound Rate Limit Module."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

import redis.asyncio as redis_async

from backend.rate_limits.contracts import (
    RateLimitPolicy,
    RateLimitSnapshot,
    RateLimitUnavailable,
)


Clock = Callable[[], float]


@dataclass(slots=True)
class _MemoryWindow:
    count: int
    reset_at: float


class InMemoryRateLimitAdapter:
    """Deterministic process-local Adapter for tests and development."""

    def __init__(self, *, clock: Clock = time.time) -> None:
        self._clock = clock
        self._windows: dict[str, _MemoryWindow] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def consume(
        self,
        *,
        key: str,
        policy: RateLimitPolicy,
        cost: int,
    ) -> RateLimitSnapshot:
        async with self._lock:
            if self._closed:
                raise RateLimitUnavailable(adapter="memory", reason="closed")
            try:
                now = float(self._clock())
            except Exception as exc:
                raise RateLimitUnavailable(
                    adapter="memory",
                    reason="clock_invalid",
                ) from exc
            if not math.isfinite(now) or now < 0:
                raise RateLimitUnavailable(adapter="memory", reason="clock_invalid")

            expired = [
                stored_key
                for stored_key, state in self._windows.items()
                if state.reset_at <= now
            ]
            for stored_key in expired:
                self._windows.pop(stored_key, None)

            state = self._windows.get(key)
            if state is None:
                window = policy.window_seconds
                reset_at = (math.floor(now / window) + 1) * window
                state = _MemoryWindow(count=0, reset_at=float(reset_at))
                self._windows[key] = state

            allowed = state.count + cost <= policy.limit
            if allowed:
                state.count += cost
            remaining = max(policy.limit - state.count, 0)
            return RateLimitSnapshot(
                allowed=allowed,
                remaining=remaining,
                reset_at=state.reset_at,
                observed_at=now,
            )

    async def close(self) -> None:
        async with self._lock:
            self._windows.clear()
            self._closed = True


_FIXED_WINDOW_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local window_start_ms = now_ms - (now_ms % window_ms)
local reset_at_ms = window_start_ms + window_ms

local stored_window_ms = tonumber(redis.call('HGET', key, 'window_start_ms'))
local count = tonumber(redis.call('HGET', key, 'count')) or 0
if stored_window_ms == nil or stored_window_ms ~= window_start_ms then
    count = 0
end

local allowed = 0
if count + cost <= limit then
    count = count + cost
    allowed = 1
end

redis.call('HSET', key, 'window_start_ms', window_start_ms, 'count', count)
redis.call('PEXPIREAT', key, reset_at_ms)

local remaining = limit - count
if remaining < 0 then
    remaining = 0
end
return {allowed, remaining, reset_at_ms, now_ms}
""".strip()


class RedisEvalClient(Protocol):
    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> Awaitable[object]: ...

    def aclose(self) -> Awaitable[None]: ...


def _response_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} cannot be boolean")
    if isinstance(value, bytes):
        value = value.decode("ascii")
    parsed = int(cast(int | str, value))
    if parsed < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return parsed


class RedisRateLimitAdapter:
    """Multi-instance Adapter using one atomic Redis Lua evaluation per check.

    URL-created clients are owned and closed by the Adapter. Injected clients
    are treated as shared unless ``close_client=True`` is explicit.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        client: RedisEvalClient | None = None,
        close_client: bool | None = None,
    ) -> None:
        if client is None:
            if not isinstance(redis_url, str) or not redis_url.strip():
                raise ValueError("redis_url is required when client is not provided")
            redis_client = redis_async.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
            self._client: RedisEvalClient = cast(RedisEvalClient, redis_client)
            self._close_client = True if close_client is None else close_client
        else:
            self._client = client
            self._close_client = False if close_client is None else close_client
        self._closed = False

    async def consume(
        self,
        *,
        key: str,
        policy: RateLimitPolicy,
        cost: int,
    ) -> RateLimitSnapshot:
        if self._closed:
            raise RateLimitUnavailable(adapter="redis", reason="closed")
        try:
            response = await self._client.eval(
                _FIXED_WINDOW_LUA,
                1,
                key,
                policy.limit,
                policy.window_seconds * 1_000,
                cost,
            )
            if not isinstance(response, (list, tuple)) or len(response) != 4:
                raise ValueError("unexpected Redis rate limit response")
            allowed_raw = _response_integer(response[0], field_name="allowed")
            remaining = _response_integer(response[1], field_name="remaining")
            reset_at_ms = _response_integer(response[2], field_name="reset_at_ms")
            observed_at_ms = _response_integer(
                response[3],
                field_name="observed_at_ms",
            )
            if allowed_raw not in {0, 1} or remaining > policy.limit:
                raise ValueError("invalid Redis rate limit response")
            return RateLimitSnapshot(
                allowed=bool(allowed_raw),
                remaining=remaining,
                reset_at=reset_at_ms / 1_000,
                observed_at=observed_at_ms / 1_000,
            )
        except RateLimitUnavailable:
            raise
        except Exception as exc:
            raise RateLimitUnavailable(adapter="redis") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_client:
            try:
                await self._client.aclose()
            except Exception as exc:
                raise RateLimitUnavailable(
                    adapter="redis",
                    reason="close_failed",
                ) from exc


__all__ = [
    "InMemoryRateLimitAdapter",
    "RedisEvalClient",
    "RedisRateLimitAdapter",
]
