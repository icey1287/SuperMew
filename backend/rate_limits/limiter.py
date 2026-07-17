"""Deep orchestration Interface for inbound rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import math
import re

from backend.rate_limits.contracts import (
    RateLimitAdapter,
    RateLimitCheck,
    RateLimitDecision,
    RateLimitPolicyMatcher,
    RateLimitUnavailable,
)
from backend.rate_limits.policy import DEFAULT_ROUTE_POLICY_MATCHER


_KEY_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class RateLimiter:
    """Select policy, hide identity, consume atomically, and derive retry data."""

    def __init__(
        self,
        adapter: RateLimitAdapter,
        *,
        identity_hmac_key: bytes | str,
        matcher: RateLimitPolicyMatcher = DEFAULT_ROUTE_POLICY_MATCHER,
        key_prefix: str = "supermew",
    ) -> None:
        key = (
            identity_hmac_key.encode("utf-8")
            if isinstance(identity_hmac_key, str)
            else identity_hmac_key
        )
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("identity_hmac_key must contain at least 32 bytes")
        if (
            not isinstance(key_prefix, str)
            or _KEY_PREFIX_RE.fullmatch(key_prefix) is None
        ):
            raise ValueError("key_prefix must be a stable identifier")
        self._adapter = adapter
        self._identity_hmac_key = key
        self._matcher = matcher
        self._key_prefix = key_prefix
        self._closed = False

    def _storage_key(self, *, policy_id: str, client_identity: str) -> str:
        material = (
            "rate-limit-identity-v1\0" + policy_id + "\0" + client_identity
        ).encode("utf-8")
        digest = hmac.new(
            self._identity_hmac_key,
            material,
            hashlib.sha256,
        ).hexdigest()
        return f"{self._key_prefix}:rate_limit:v1:{policy_id}:{digest}"

    async def check(self, check: RateLimitCheck) -> RateLimitDecision:
        if self._closed:
            raise RateLimitUnavailable(adapter="limiter", reason="closed")
        if not isinstance(check, RateLimitCheck):
            raise TypeError("check must be a RateLimitCheck")
        policy = self._matcher.match(method=check.method, path=check.path)
        snapshot = await self._adapter.consume(
            key=self._storage_key(
                policy_id=policy.id,
                client_identity=check.client_identity,
            ),
            policy=policy,
            cost=check.cost,
        )
        if snapshot.remaining > policy.limit:
            raise RateLimitUnavailable(
                adapter="limiter",
                reason="invalid_adapter_response",
            )
        retry_after = (
            0
            if snapshot.allowed
            else max(1, math.ceil(snapshot.reset_at - snapshot.observed_at))
        )
        return RateLimitDecision(
            policy_id=policy.id,
            allowed=snapshot.allowed,
            limit=policy.limit,
            remaining=snapshot.remaining,
            retry_after=retry_after,
            reset=math.ceil(snapshot.reset_at),
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._adapter.close()


__all__ = ["RateLimiter"]
