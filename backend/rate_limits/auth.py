"""Auth-specific secondary limit enforced before password verification."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, cast

from fastapi import Request

from backend.auth.identity import normalize_username_for_rate_limit
from backend.core.errors import AppError, ErrorCode
from backend.rate_limits.contracts import (
    RateLimitCheck,
    RateLimitDecision,
    RateLimitUnavailable,
)


class AuthRateLimiter(Protocol):
    async def check(self, check: RateLimitCheck) -> RateLimitDecision: ...


_AUTH_IDENTITY_COST = 2


def _bounded_username_identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = normalize_username_for_rate_limit(value)
    if not normalized:
        return None
    encoded = normalized.encode("utf-8")
    if len(encoded) <= 1_024:
        return normalized
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _direct_client_host(request: Request) -> str:
    client = request.client
    if client is None or not client.host:
        return "unknown"
    host = client.host.strip()
    if not host or len(host.encode("utf-8")) > 512:
        return "unknown"
    return host


async def enforce_auth_username_rate_limit(request: Request) -> None:
    """Consume the IP+username policy before synchronous password hashing runs."""

    limiter = cast(
        AuthRateLimiter | None,
        getattr(request.app.state, "rate_limiter", None),
    )
    if limiter is None:
        return
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    username = _bounded_username_identity(payload.get("username"))
    if username is None:
        return

    try:
        decision = await limiter.check(
            RateLimitCheck(
                method=request.method,
                path=request.url.path,
                client_identity=(
                    f"host:{_direct_client_host(request)}\0username:{username}"
                ),
                cost=_AUTH_IDENTITY_COST,
            )
        )
    except RateLimitUnavailable as exc:
        raise AppError(
            ErrorCode.RATE_LIMIT_UNAVAILABLE,
            "请求限流服务暂时不可用，请稍后重试",
            status_code=503,
            retryable=True,
            category="rate_limit",
            stage="auth_identity",
            retry_after=1,
        ) from exc

    if not decision.allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "请求过于频繁，请稍后重试",
            status_code=429,
            retryable=True,
            safe_details={
                "limit": decision.limit,
                "remaining": decision.remaining,
                "reset": decision.reset,
            },
            category="rate_limit",
            stage=decision.policy_id,
            retry_after=float(decision.retry_after),
        )


__all__ = ["AuthRateLimiter", "enforce_auth_username_rate_limit"]
