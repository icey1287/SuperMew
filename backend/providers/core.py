from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, TypeVar

from backend.core.errors import AppError


T = TypeVar("T")
CancellationProbe = Callable[[], bool]


class ProviderCode(StrEnum):
    """Stable public failure codes for external provider calls."""

    EMBEDDING_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
    VECTOR_STORE_UNAVAILABLE = "VECTOR_STORE_UNAVAILABLE"
    RERANK_TIMEOUT = "RERANK_TIMEOUT"
    RERANK_RATE_LIMITED = "RERANK_RATE_LIMITED"
    RERANK_UNAVAILABLE = "RERANK_UNAVAILABLE"
    RERANK_INVALID_RESPONSE = "RERANK_INVALID_RESPONSE"
    RERANK_CIRCUIT_OPEN = "RERANK_CIRCUIT_OPEN"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    PROVIDER_AUTHENTICATION_FAILED = "PROVIDER_AUTHENTICATION_FAILED"
    PROVIDER_REQUEST_INVALID = "PROVIDER_REQUEST_INVALID"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_DEADLINE_EXCEEDED = "PROVIDER_TIMEOUT"
    POLICY_DENIED = "POLICY_DENIED"


class ProviderOperation(StrEnum):
    EMBEDDING = "embedding"
    VECTOR_SEARCH = "vector_search"
    RERANK = "rerank"
    MODEL = "model"
    TOOL = "tool"


@dataclass(frozen=True)
class _ProviderCodeSpec:
    message: str
    status_code: int
    retryable: bool


_CODE_SPECS: dict[ProviderCode, _ProviderCodeSpec] = {
    ProviderCode.EMBEDDING_UNAVAILABLE: _ProviderCodeSpec(
        "嵌入服务暂时不可用，请稍后重试", 503, True
    ),
    ProviderCode.VECTOR_STORE_UNAVAILABLE: _ProviderCodeSpec(
        "向量检索服务暂时不可用，请稍后重试", 503, True
    ),
    ProviderCode.RERANK_TIMEOUT: _ProviderCodeSpec(
        "精排服务响应超时，请稍后重试", 504, True
    ),
    ProviderCode.RERANK_RATE_LIMITED: _ProviderCodeSpec(
        "精排服务当前繁忙，请稍后重试", 429, True
    ),
    ProviderCode.RERANK_UNAVAILABLE: _ProviderCodeSpec(
        "精排服务暂时不可用，请稍后重试", 503, True
    ),
    ProviderCode.RERANK_INVALID_RESPONSE: _ProviderCodeSpec(
        "精排服务返回了无效结果，请联系管理员", 502, False
    ),
    ProviderCode.RERANK_CIRCUIT_OPEN: _ProviderCodeSpec(
        "精排服务暂时不可用，已启用召回排序降级", 503, True
    ),
    ProviderCode.MODEL_TIMEOUT: _ProviderCodeSpec(
        "模型服务响应超时，请稍后重试", 504, True
    ),
    ProviderCode.MODEL_RATE_LIMITED: _ProviderCodeSpec(
        "上游模型服务当前繁忙，请稍后重试", 429, True
    ),
    ProviderCode.MODEL_UNAVAILABLE: _ProviderCodeSpec(
        "模型服务暂时不可用，请稍后重试", 503, True
    ),
    ProviderCode.TOOL_TIMEOUT: _ProviderCodeSpec("工具执行超时，请稍后重试", 504, True),
    ProviderCode.TOOL_UNAVAILABLE: _ProviderCodeSpec(
        "工具服务暂时不可用，请稍后重试", 503, True
    ),
    ProviderCode.PROVIDER_AUTHENTICATION_FAILED: _ProviderCodeSpec(
        "上游服务配置不可用，请联系管理员", 503, False
    ),
    ProviderCode.PROVIDER_REQUEST_INVALID: _ProviderCodeSpec(
        "上游服务拒绝了当前请求，请联系管理员", 502, False
    ),
    ProviderCode.PROVIDER_TIMEOUT: _ProviderCodeSpec(
        "运行截止时间已到，已停止等待上游服务", 504, False
    ),
    ProviderCode.POLICY_DENIED: _ProviderCodeSpec(
        "当前操作不被安全策略允许", 403, False
    ),
}


@dataclass(frozen=True)
class _OperationProfile:
    timeout: ProviderCode
    rate_limited: ProviderCode
    unavailable: ProviderCode


_OPERATION_PROFILES: dict[ProviderOperation, _OperationProfile] = {
    ProviderOperation.EMBEDDING: _OperationProfile(
        timeout=ProviderCode.EMBEDDING_UNAVAILABLE,
        rate_limited=ProviderCode.EMBEDDING_UNAVAILABLE,
        unavailable=ProviderCode.EMBEDDING_UNAVAILABLE,
    ),
    ProviderOperation.VECTOR_SEARCH: _OperationProfile(
        timeout=ProviderCode.VECTOR_STORE_UNAVAILABLE,
        rate_limited=ProviderCode.VECTOR_STORE_UNAVAILABLE,
        unavailable=ProviderCode.VECTOR_STORE_UNAVAILABLE,
    ),
    ProviderOperation.RERANK: _OperationProfile(
        timeout=ProviderCode.RERANK_TIMEOUT,
        rate_limited=ProviderCode.RERANK_RATE_LIMITED,
        unavailable=ProviderCode.RERANK_UNAVAILABLE,
    ),
    ProviderOperation.MODEL: _OperationProfile(
        timeout=ProviderCode.MODEL_TIMEOUT,
        rate_limited=ProviderCode.MODEL_RATE_LIMITED,
        unavailable=ProviderCode.MODEL_UNAVAILABLE,
    ),
    ProviderOperation.TOOL: _OperationProfile(
        timeout=ProviderCode.TOOL_TIMEOUT,
        rate_limited=ProviderCode.TOOL_UNAVAILABLE,
        unavailable=ProviderCode.TOOL_UNAVAILABLE,
    ),
}


def _safe_identifier(value: str, *, fallback: str) -> str:
    compact = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", compact):
        return compact
    return fallback


def _finite_non_negative(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


@dataclass(frozen=True)
class ProviderCallContext:
    provider: str
    operation: ProviderOperation
    deadline: float | None = None
    cancellation: CancellationProbe | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", ProviderOperation(self.operation))
        if self.deadline is not None and not math.isfinite(float(self.deadline)):
            raise ValueError("deadline must be a finite monotonic timestamp")
        if self.cancellation is not None and not callable(self.cancellation):
            raise TypeError("cancellation must be callable")


@dataclass(frozen=True)
class ProviderPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.1
    max_backoff_seconds: float = 2.0
    max_retry_after_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    cancellation_poll_seconds: float = 0.05

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        numeric = {
            "initial_backoff_seconds": self.initial_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
            "max_retry_after_seconds": self.max_retry_after_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "cancellation_poll_seconds": self.cancellation_poll_seconds,
        }
        if any(not math.isfinite(float(value)) for value in numeric.values()):
            raise ValueError("provider retry policy values must be finite")
        if (
            self.initial_backoff_seconds < 0
            or self.max_backoff_seconds < 0
            or self.max_retry_after_seconds < 0
        ):
            raise ValueError("provider backoff values cannot be negative")
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("initial backoff cannot exceed max backoff")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")
        if self.cancellation_poll_seconds <= 0:
            raise ValueError("cancellation_poll_seconds must be positive")

    def retry_delay(
        self, failed_attempt: int, retry_after_seconds: float | None
    ) -> float:
        try:
            exponential = self.initial_backoff_seconds * (
                self.backoff_multiplier ** max(failed_attempt - 1, 0)
            )
        except OverflowError:
            exponential = self.max_backoff_seconds
        delay = min(exponential, self.max_backoff_seconds)
        if retry_after_seconds is not None:
            delay = max(delay, retry_after_seconds)
        return delay


class ProviderError(AppError):
    """A redacted, stable provider failure that can cross Run and HTTP seams."""

    def __init__(
        self,
        code: ProviderCode | str,
        *,
        context: ProviderCallContext,
        retry_after_seconds: float | None = None,
        attempts: int = 1,
        max_attempts: int = 1,
    ) -> None:
        resolved_code = ProviderCode(code)
        spec = _CODE_SPECS[resolved_code]
        retry_after = _finite_non_negative(retry_after_seconds)
        provider = _safe_identifier(context.provider, fallback="unknown-provider")
        operation = context.operation.value
        safe_details: dict[str, Any] = {
            "provider": provider,
            "operation": operation,
            "attempts": max(int(attempts), 1),
            "max_attempts": max(int(max_attempts), 1),
        }
        if retry_after is not None:
            safe_details["retry_after_seconds"] = retry_after
        super().__init__(
            resolved_code,
            spec.message,
            status_code=spec.status_code,
            retryable=spec.retryable,
            safe_details=safe_details,
            category="provider",
            stage=operation,
            provider=provider,
            retry_after=retry_after,
        )
        self.provider = provider
        self.operation = context.operation
        self.retry_after_seconds = retry_after
        self.attempts = safe_details["attempts"]
        self.max_attempts = safe_details["max_attempts"]

    def with_attempts(self, attempts: int, max_attempts: int) -> ProviderError:
        return ProviderError(
            self.code,
            context=ProviderCallContext(
                provider=self.provider,
                operation=self.operation,
            ),
            retry_after_seconds=self.retry_after_seconds,
            attempts=attempts,
            max_attempts=max_attempts,
        )

    @classmethod
    def from_code(
        cls,
        code: ProviderCode | str,
        *,
        provider: str,
        operation: ProviderOperation | str,
        retry_after_seconds: float | None = None,
        attempts: int = 1,
        max_attempts: int = 1,
    ) -> ProviderError:
        """Rebuild a typed error from JSON-safe provider failure fields."""

        return cls(
            code,
            context=ProviderCallContext(
                provider=provider,
                operation=ProviderOperation(operation),
            ),
            retry_after_seconds=retry_after_seconds,
            attempts=attempts,
            max_attempts=max_attempts,
        )

    def to_snapshot(self) -> dict[str, Any]:
        """Return the complete safe state needed to re-raise after checkpointing."""

        return {
            "code": getattr(self.code, "value", str(self.code)),
            "provider": self.provider,
            "operation": self.operation.value,
            "retry_after_seconds": self.retry_after_seconds,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any]) -> ProviderError:
        return cls.from_code(
            payload["code"],
            provider=str(payload.get("provider") or "unknown-provider"),
            operation=payload["operation"],
            retry_after_seconds=payload.get("retry_after_seconds"),
            attempts=int(payload.get("attempts") or 1),
            max_attempts=int(payload.get("max_attempts") or 1),
        )

    @classmethod
    def policy_denied(cls, context: ProviderCallContext) -> ProviderError:
        return cls(ProviderCode.POLICY_DENIED, context=context)

    @classmethod
    def deadline_exceeded(
        cls,
        context: ProviderCallContext,
        *,
        attempts: int = 1,
        max_attempts: int = 1,
    ) -> ProviderError:
        return cls(
            ProviderCode.PROVIDER_TIMEOUT,
            context=context,
            attempts=attempts,
            max_attempts=max_attempts,
        )


def _status_code(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        for source in (item, getattr(item, "response", None)):
            if source is None:
                continue
            for name in ("status_code", "status"):
                value = getattr(source, name, None)
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if 100 <= parsed <= 599:
                    return parsed
    return None


def _headers(exc: BaseException) -> Any:
    for item in _exception_chain(exc):
        direct = getattr(item, "headers", None)
        if direct is not None:
            return direct
        response = getattr(item, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            return headers
    return None


def _header_value(headers: Any, name: str) -> Any:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        return value
    return None


def _parse_retry_after(value: Any) -> float | None:
    parsed = _finite_non_negative(value)
    if parsed is not None:
        return parsed
    if not isinstance(value, str):
        return None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds(), 0.0)


def _retry_after_seconds(exc: BaseException) -> float | None:
    for item in _exception_chain(exc):
        for value in (
            getattr(item, "retry_after_seconds", None),
            getattr(item, "retry_after", None),
            _header_value(_headers(item), "Retry-After"),
        ):
            parsed = _parse_retry_after(value)
            if parsed is not None:
                return parsed
    return None


_TIMEOUT_TYPE_NAMES = {
    "APITimeoutError",
    "TimeoutException",
    "Timeout",
    "TimeoutError",
    "ConnectTimeout",
    "ConnectTimeoutError",
    "ReadTimeout",
    "ReadTimeoutError",
    "ServerTimeoutError",
    "SocketTimeout",
    "WriteTimeout",
    "PoolTimeout",
}


def _exception_chain(exc: BaseException):
    """Walk explicit exception chaining without inspecting provider messages."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_timeout(exc: BaseException, status_code: int | None) -> bool:
    if status_code in {408, 504}:
        return True
    return any(
        isinstance(item, TimeoutError)
        or any(base.__name__ in _TIMEOUT_TYPE_NAMES for base in type(item).__mro__)
        for item in _exception_chain(exc)
    )


def _is_policy_error(exc: BaseException) -> bool:
    if not isinstance(exc, AppError):
        return False
    code = getattr(exc.code, "value", str(exc.code))
    return code in {"POLICY_DENIED", "PERMISSION_DENIED"}


def classify_provider_exception(
    exc: BaseException,
    *,
    context: ProviderCallContext,
    attempts: int = 1,
    max_attempts: int = 1,
) -> ProviderError:
    """Normalize provider-specific exceptions without inspecting raw message text."""

    if isinstance(exc, asyncio.CancelledError):
        raise exc
    if not isinstance(exc, Exception):
        raise exc
    if isinstance(exc, ProviderError):
        return exc.with_attempts(attempts, max_attempts)
    if _is_policy_error(exc):
        return ProviderError(
            ProviderCode.POLICY_DENIED,
            context=context,
            attempts=attempts,
            max_attempts=max_attempts,
        )

    profile = _OPERATION_PROFILES[context.operation]
    status_code = _status_code(exc)
    retry_after = _retry_after_seconds(exc)
    if status_code == 429:
        code = profile.rate_limited
    elif status_code in {401, 403}:
        code = ProviderCode.PROVIDER_AUTHENTICATION_FAILED
    elif status_code is not None and 400 <= status_code < 500 and status_code != 408:
        code = ProviderCode.PROVIDER_REQUEST_INVALID
    elif _is_timeout(exc, status_code):
        code = profile.timeout
    else:
        code = profile.unavailable
    return ProviderError(
        code,
        context=context,
        retry_after_seconds=retry_after,
        attempts=attempts,
        max_attempts=max_attempts,
    )


class ProviderExecutor:
    """Execute sync or async provider calls with one retry/error interface."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        async_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._async_sleeper = async_sleeper

    def call(
        self,
        fn: Callable[[], T],
        *,
        context: ProviderCallContext,
        policy: ProviderPolicy = ProviderPolicy(),
    ) -> T:
        for attempt in range(1, policy.max_attempts + 1):
            self._checkpoint(context, attempt, policy.max_attempts)
            try:
                result = fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = classify_provider_exception(
                    exc,
                    context=context,
                    attempts=attempt,
                    max_attempts=policy.max_attempts,
                )
                if not error.retryable or attempt >= policy.max_attempts:
                    raise error from exc
                if (
                    error.retry_after_seconds is not None
                    and error.retry_after_seconds > policy.max_retry_after_seconds
                ):
                    raise error from exc
                delay = policy.retry_delay(attempt, error.retry_after_seconds)
                self._ensure_retry_fits_deadline(
                    context,
                    delay,
                    attempts=attempt,
                    max_attempts=policy.max_attempts,
                    cause=error,
                )
                self._sleep_with_cancellation(
                    delay,
                    context,
                    policy,
                    attempts=attempt,
                )
                continue
            self._checkpoint(context, attempt, policy.max_attempts)
            return result
        raise AssertionError("provider retry loop exhausted without a result")

    async def acall(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        context: ProviderCallContext,
        policy: ProviderPolicy = ProviderPolicy(),
    ) -> T:
        for attempt in range(1, policy.max_attempts + 1):
            self._checkpoint(context, attempt, policy.max_attempts)
            timeout_scope = None
            try:
                remaining = self._remaining(context)
                if remaining is None:
                    result = await self._await_with_cancellation(
                        fn(),
                        context=context,
                        policy=policy,
                    )
                else:
                    async with asyncio.timeout(remaining) as timeout_scope:
                        result = await self._await_with_cancellation(
                            fn(),
                            context=context,
                            policy=policy,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if timeout_scope is not None and timeout_scope.expired():
                    error = ProviderError.deadline_exceeded(
                        context,
                        attempts=attempt,
                        max_attempts=policy.max_attempts,
                    )
                else:
                    error = classify_provider_exception(
                        exc,
                        context=context,
                        attempts=attempt,
                        max_attempts=policy.max_attempts,
                    )
                if not error.retryable or attempt >= policy.max_attempts:
                    raise error from exc
                if (
                    error.retry_after_seconds is not None
                    and error.retry_after_seconds > policy.max_retry_after_seconds
                ):
                    raise error from exc
                delay = policy.retry_delay(attempt, error.retry_after_seconds)
                self._ensure_retry_fits_deadline(
                    context,
                    delay,
                    attempts=attempt,
                    max_attempts=policy.max_attempts,
                    cause=error,
                )
                await self._asleep_with_cancellation(
                    delay,
                    context,
                    policy,
                    attempts=attempt,
                )
                continue
            self._checkpoint(context, attempt, policy.max_attempts)
            return result
        raise AssertionError("provider retry loop exhausted without a result")

    async def _await_with_cancellation(
        self,
        awaitable: Awaitable[T],
        *,
        context: ProviderCallContext,
        policy: ProviderPolicy,
    ) -> T:
        task = asyncio.ensure_future(awaitable)
        if context.cancellation is None:
            return await task

        cancellation_task = asyncio.create_task(
            self._wait_until_cancelled(context, policy)
        )
        try:
            done, _ = await asyncio.wait(
                {task, cancellation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return await task
            try:
                await cancellation_task
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.CancelledError("provider call cancelled")
        except BaseException:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)

    async def _wait_until_cancelled(
        self,
        context: ProviderCallContext,
        policy: ProviderPolicy,
    ) -> None:
        while not self._cancelled(context):
            # Cancellation observation must always yield to the provider task.
            # Retry sleepers are injectable and may intentionally return
            # immediately in tests, so they cannot safely drive this watcher.
            await asyncio.sleep(policy.cancellation_poll_seconds)

    def _remaining(self, context: ProviderCallContext) -> float | None:
        if context.deadline is None:
            return None
        return max(float(context.deadline) - self._clock(), 0.0)

    @staticmethod
    def _cancelled(context: ProviderCallContext) -> bool:
        return bool(context.cancellation and context.cancellation())

    def _checkpoint(
        self,
        context: ProviderCallContext,
        attempts: int,
        max_attempts: int,
    ) -> None:
        if self._cancelled(context):
            raise asyncio.CancelledError("provider call cancelled")
        remaining = self._remaining(context)
        if remaining is not None and remaining <= 0:
            raise ProviderError.deadline_exceeded(
                context,
                attempts=attempts,
                max_attempts=max_attempts,
            )

    def _ensure_retry_fits_deadline(
        self,
        context: ProviderCallContext,
        delay: float,
        *,
        attempts: int,
        max_attempts: int,
        cause: ProviderError,
    ) -> None:
        remaining = self._remaining(context)
        if remaining is not None and delay >= remaining:
            raise ProviderError.deadline_exceeded(
                context,
                attempts=attempts,
                max_attempts=max_attempts,
            ) from cause

    def _sleep_with_cancellation(
        self,
        delay: float,
        context: ProviderCallContext,
        policy: ProviderPolicy,
        *,
        attempts: int,
    ) -> None:
        if delay <= 0:
            self._checkpoint(context, attempts, policy.max_attempts)
            return
        if context.cancellation is None:
            self._sleeper(delay)
            return
        remaining = delay
        while remaining > 0:
            self._checkpoint(context, attempts, policy.max_attempts)
            interval = min(remaining, policy.cancellation_poll_seconds)
            self._sleeper(interval)
            remaining -= interval
        self._checkpoint(context, attempts, policy.max_attempts)

    async def _asleep_with_cancellation(
        self,
        delay: float,
        context: ProviderCallContext,
        policy: ProviderPolicy,
        *,
        attempts: int,
    ) -> None:
        if delay <= 0:
            self._checkpoint(context, attempts, policy.max_attempts)
            return
        if context.cancellation is None:
            await self._async_sleeper(delay)
            return
        remaining = delay
        while remaining > 0:
            self._checkpoint(context, attempts, policy.max_attempts)
            interval = min(remaining, policy.cancellation_poll_seconds)
            await self._async_sleeper(interval)
            remaining -= interval
        self._checkpoint(context, attempts, policy.max_attempts)


provider_executor = ProviderExecutor()
