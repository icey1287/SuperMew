"""Application-owned construction for the inbound Rate Limit Module."""

from __future__ import annotations

import secrets

from backend.core.settings import AppSettings
from backend.rate_limits.adapters import (
    InMemoryRateLimitAdapter,
    RedisRateLimitAdapter,
)
from backend.rate_limits.contracts import RateLimitAdapter
from backend.rate_limits.limiter import RateLimiter


def build_rate_limiter(settings: AppSettings) -> RateLimiter | None:
    configured = getattr(settings, "rate_limits", None)
    if configured is None or not configured.enabled:
        return None

    raw_key = configured.identity_hmac_key.get_secret_value().strip()
    if not raw_key:
        if configured.backend == "redis":
            raise ValueError("Redis Rate Limit requires a stable RATE_LIMIT_HMAC_KEY")
        identity_key: bytes | str = secrets.token_bytes(32)
    else:
        identity_key = raw_key

    adapter: RateLimitAdapter
    if configured.backend == "memory":
        adapter = InMemoryRateLimitAdapter()
    elif configured.backend == "redis":
        adapter = RedisRateLimitAdapter(settings.storage.redis_url.get_secret_value())
    else:  # pragma: no cover - Pydantic rejects unknown values first.
        raise ValueError("unsupported Rate Limit backend")

    return RateLimiter(
        adapter,
        identity_hmac_key=identity_key,
        key_prefix=configured.key_prefix,
    )


__all__ = ["build_rate_limiter"]
