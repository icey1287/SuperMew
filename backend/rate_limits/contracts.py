"""Stable contracts for inbound request rate limiting.

Raw client identities cross the public Interface only long enough to be HMAC
fingerprinted by :class:`RateLimiter`.  Storage Adapters receive opaque keys
and never need to understand usernames, bearer tokens, IP addresses, or route
parameters.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


_POLICY_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_METHOD_RE = re.compile(r"^[A-Z]{3,12}$")
_SAFE_LABEL_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_IDENTITY_BYTES = 4_096
_MAX_PATH_BYTES = 2_048


def _positive_int(value: int, *, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum}")
    return value


def _finite_timestamp(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return normalized


class RateLimitErrorCode(StrEnum):
    UNAVAILABLE = "RATE_LIMIT_UNAVAILABLE"


class RateLimitUnavailable(RuntimeError):
    """Typed fail-closed storage failure with no request identity attached."""

    code = RateLimitErrorCode.UNAVAILABLE

    def __init__(self, *, adapter: str, reason: str = "unavailable") -> None:
        safe_adapter = (
            adapter
            if isinstance(adapter, str) and _SAFE_LABEL_RE.fullmatch(adapter)
            else "unknown"
        )
        safe_reason = (
            reason
            if isinstance(reason, str) and _SAFE_LABEL_RE.fullmatch(reason)
            else "unavailable"
        )
        self.adapter = safe_adapter
        self.reason = safe_reason
        self.retryable = True
        self.safe_details = {"adapter": safe_adapter, "reason": safe_reason}
        super().__init__("Rate limit storage is temporarily unavailable")


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """One immutable fixed-window policy selected by the route matcher."""

    id: str
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _POLICY_ID_RE.fullmatch(self.id) is None:
            raise ValueError("policy id must be a stable lowercase identifier")
        _positive_int(self.limit, field_name="limit", maximum=1_000_000)
        _positive_int(
            self.window_seconds,
            field_name="window_seconds",
            maximum=86_400,
        )


@dataclass(frozen=True, slots=True)
class RateLimitCheck:
    """Public check input; repr deliberately redacts the client identity."""

    method: str
    path: str
    client_identity: str = field(repr=False)
    cost: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.method, str):
            raise TypeError("method must be a string")
        method = self.method.strip().upper()
        if _METHOD_RE.fullmatch(method) is None:
            raise ValueError("method must be an uppercase HTTP method")
        object.__setattr__(self, "method", method)

        if not isinstance(self.path, str):
            raise TypeError("path must be a string")
        path = self.path.split("?", 1)[0]
        try:
            path_bytes = path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("path contains invalid Unicode") from exc
        if (
            not path.startswith("/")
            or len(path_bytes) > _MAX_PATH_BYTES
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in path
            )
        ):
            raise ValueError("path must be a bounded absolute request path")
        object.__setattr__(self, "path", path)

        if not isinstance(self.client_identity, str):
            raise TypeError("client_identity must be a string")
        try:
            identity_bytes = self.client_identity.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("client_identity contains invalid Unicode") from exc
        if not identity_bytes or len(identity_bytes) > _MAX_IDENTITY_BYTES:
            raise ValueError("client_identity must be non-empty and bounded")

        _positive_int(self.cost, field_name="cost", maximum=1_000_000)


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """Internal Adapter result before HTTP-facing retry fields are derived."""

    allowed: bool
    remaining: int
    reset_at: float
    observed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be a boolean")
        if (
            isinstance(self.remaining, bool)
            or not isinstance(self.remaining, int)
            or self.remaining < 0
        ):
            raise ValueError("remaining must be a non-negative integer")
        reset_at = _finite_timestamp(self.reset_at, field_name="reset_at")
        observed_at = _finite_timestamp(self.observed_at, field_name="observed_at")
        if reset_at < observed_at:
            raise ValueError("reset_at must not precede observed_at")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """HTTP-neutral decision; ``reset`` is a UNIX epoch second."""

    policy_id: str
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset: int

    def __post_init__(self) -> None:
        if _POLICY_ID_RE.fullmatch(self.policy_id) is None:
            raise ValueError("policy_id must be a stable identifier")
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be a boolean")
        _positive_int(self.limit, field_name="limit", maximum=1_000_000)
        if isinstance(self.remaining, bool) or not isinstance(self.remaining, int):
            raise TypeError("remaining must be an integer")
        if not 0 <= self.remaining <= self.limit:
            raise ValueError("remaining must be between zero and limit")
        if (
            isinstance(self.retry_after, bool)
            or not isinstance(self.retry_after, int)
            or isinstance(self.reset, bool)
            or not isinstance(self.reset, int)
        ):
            raise TypeError("retry_after and reset must be integers")
        if self.retry_after < 0 or self.reset < 0:
            raise ValueError("retry_after and reset must not be negative")


class RateLimitAdapter(Protocol):
    """Internal storage seam shared by in-memory and Redis Adapters."""

    def consume(
        self,
        *,
        key: str,
        policy: RateLimitPolicy,
        cost: int,
    ) -> Awaitable[RateLimitSnapshot]: ...

    def close(self) -> Awaitable[None]: ...


class RateLimitPolicyMatcher(Protocol):
    def match(self, *, method: str, path: str) -> RateLimitPolicy: ...


__all__ = [
    "RateLimitAdapter",
    "RateLimitCheck",
    "RateLimitDecision",
    "RateLimitErrorCode",
    "RateLimitPolicy",
    "RateLimitPolicyMatcher",
    "RateLimitSnapshot",
    "RateLimitUnavailable",
]
