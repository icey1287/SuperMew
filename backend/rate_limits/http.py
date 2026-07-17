"""Pure ASGI Adapter for the inbound Rate Limit Module.

The middleware never reads a request body and never forwards raw credentials.
It submits only the direct client host or a verified, stable access-token
subject to ``RateLimiter``, which HMACs that identity before calling storage.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.core.errors import ErrorCode, PublicError, error_payload
from backend.rate_limits.contracts import (
    RateLimitCheck,
    RateLimitDecision,
    RateLimitUnavailable,
)


_SKIP_EXACT: Final = frozenset(
    {
        "/",
        "/docs",
        "/favicon.ico",
        "/health",
        "/health/live",
        "/health/ready",
        "/openapi.json",
        "/redoc",
        "/robots.txt",
        "/index.html",
        "/v1/health",
    }
)
_SKIP_PREFIXES: Final = (
    "/assets/",
    "/docs/",
    "/redoc/",
    "/static/",
    "/v1/health/",
)
_HOST_ONLY_PATHS: Final = frozenset(
    {"/auth/login", "/auth/register", "/auth/refresh", "/auth/logout"}
)
_MAX_RAW_IDENTITY_BYTES: Final = 4_080
_MAX_REQUEST_PATH_BYTES: Final = 2_048
_VALIDATION_IDENTITY: Final = "host:request-target-validation"
_RATE_LIMIT_HEADER_NAMES: Final = frozenset(
    {b"ratelimit-limit", b"ratelimit-remaining", b"ratelimit-reset"}
)


class HttpRateLimiter(Protocol):
    def check(self, check: RateLimitCheck) -> Awaitable[RateLimitDecision]: ...


BearerSubjectResolver = Callable[[str], str | None]


def _normalized_path(scope: Scope) -> str:
    value = scope.get("path", "/")
    return value if isinstance(value, str) else ""


def _must_skip(*, method: str, path: str) -> bool:
    if method == "OPTIONS" or path in _SKIP_EXACT:
        return True
    if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return True
    return False


def _bounded_secret(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_RAW_IDENTITY_BYTES or any(
        character.isspace() for character in value
    ):
        return None
    return value


def _bounded_subject(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > 1_024 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        return None
    return value


def _client_host(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        host = client[0]
        if isinstance(host, str) and _bounded_secret(host) is not None:
            return host
    return "unknown"


def _bearer_token(headers: Headers) -> str | None:
    authorization = headers.get("authorization")
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        return None
    return _bounded_secret(parts[1])


def _client_identity(
    scope: Scope,
    *,
    path: str,
    headers: Headers,
    bearer_subject_resolver: BearerSubjectResolver | None,
) -> str:
    host_identity = f"host:{_client_host(scope)}"
    route_path = path.rstrip("/") or "/"
    if route_path in _HOST_ONLY_PATHS:
        # Opaque refresh credentials rotate on every successful refresh, so
        # cookie-backed actions cannot derive a stable credential bucket.
        return host_identity
    bearer = _bearer_token(headers)
    if bearer is None or bearer_subject_resolver is None:
        return host_identity
    try:
        subject = _bounded_subject(bearer_subject_resolver(bearer))
    except Exception:
        subject = None
    return f"subject:{subject}" if subject is not None else host_identity


def _rate_limit_headers(decision: RateLimitDecision) -> list[tuple[bytes, bytes]]:
    return [
        (b"ratelimit-limit", str(decision.limit).encode("ascii")),
        (b"ratelimit-remaining", str(decision.remaining).encode("ascii")),
        (b"ratelimit-reset", str(decision.reset).encode("ascii")),
    ]


def _request_id(scope: Scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get("request_id")
    return value if isinstance(value, str) and value else None


def _limited_response(
    scope: Scope,
    decision: RateLimitDecision,
) -> JSONResponse:
    public = PublicError(
        ErrorCode.RATE_LIMITED,
        "请求过于频繁，请稍后重试",
        status_code=429,
        retryable=True,
        category="rate_limit",
        stage=decision.policy_id,
        retry_after=float(decision.retry_after),
        details={
            "limit": decision.limit,
            "remaining": decision.remaining,
            "reset": decision.reset,
        },
    )
    headers = {
        "Retry-After": str(decision.retry_after),
        "RateLimit-Limit": str(decision.limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Reset": str(decision.reset),
    }
    return JSONResponse(
        status_code=429,
        content=error_payload(public, _request_id(scope)),
        headers=headers,
    )


def _unavailable_response(scope: Scope) -> JSONResponse:
    public = PublicError(
        ErrorCode.RATE_LIMIT_UNAVAILABLE,
        "请求限流服务暂时不可用，请稍后重试",
        status_code=503,
        retryable=True,
        category="rate_limit",
        stage="request",
        retry_after=1.0,
    )
    return JSONResponse(
        status_code=503,
        content=error_payload(public, _request_id(scope)),
        headers={"Retry-After": "1"},
    )


def _invalid_request_response(scope: Scope, *, path: str) -> JSONResponse:
    try:
        path_too_long = len(path.encode("utf-8")) > _MAX_REQUEST_PATH_BYTES
    except UnicodeEncodeError:
        path_too_long = False
    status_code = 414 if path_too_long else 400
    public = PublicError(
        ErrorCode.INVALID_REQUEST,
        "请求路径过长" if path_too_long else "请求路径无效",
        status_code=status_code,
        retryable=False,
        category="request",
        stage="path",
    )
    return JSONResponse(
        status_code=status_code,
        content=error_payload(public, _request_id(scope)),
    )


def _with_rate_limit_headers(
    send: Send,
    decision: RateLimitDecision,
) -> Send:
    additions = _rate_limit_headers(decision)

    async def send_with_headers(message: Message) -> None:
        if message["type"] == "http.response.start":
            existing = [
                (name, value)
                for name, value in message.get("headers", [])
                if name.lower() not in _RATE_LIMIT_HEADER_NAMES
            ]
            message = {**message, "headers": [*existing, *additions]}
        await send(message)

    return send_with_headers


class RateLimitMiddleware:
    """Injectable pure ASGI Adapter; the app factory owns its lifecycle."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: HttpRateLimiter,
        bearer_subject_resolver: BearerSubjectResolver | None = None,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.bearer_subject_resolver = bearer_subject_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method_value = scope.get("method", "GET")
        method = method_value.upper() if isinstance(method_value, str) else "GET"
        path = _normalized_path(scope)
        try:
            validated_target = RateLimitCheck(
                method=method,
                path=path,
                client_identity=_VALIDATION_IDENTITY,
            )
        except (TypeError, ValueError):
            response = _invalid_request_response(scope, path=path)
            await response(scope, receive, send)
            return
        method = validated_target.method
        path = validated_target.path
        if _must_skip(method=method, path=path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        identity = _client_identity(
            scope,
            path=path,
            headers=headers,
            bearer_subject_resolver=self.bearer_subject_resolver,
        )
        try:
            check = RateLimitCheck(
                method=method,
                path=path,
                client_identity=identity,
            )
        except (TypeError, ValueError):
            response = _invalid_request_response(scope, path=path)
            await response(scope, receive, send)
            return

        try:
            decision = await self.limiter.check(check)
        except RateLimitUnavailable:
            response = _unavailable_response(scope)
            await response(scope, receive, send)
            return

        if not decision.allowed:
            response = _limited_response(scope, decision)
            await response(scope, receive, send)
            return
        await self.app(
            scope,
            receive,
            _with_rate_limit_headers(send, decision),
        )


__all__ = [
    "BearerSubjectResolver",
    "HttpRateLimiter",
    "RateLimitMiddleware",
]
