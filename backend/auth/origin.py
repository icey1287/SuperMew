"""Browser-origin and bounded-body protection for authentication requests."""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.core.errors import ErrorCode, PublicError, error_payload
from backend.core.settings import SecuritySettings
from backend.security.origins import canonical_http_origin


AUTH_REQUEST_BODY_LIMIT = 16 * 1024
_AUTH_PATH_PREFIX = "/auth/"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_JSON_BODY_PATHS = frozenset({"/auth/login", "/auth/register"})
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
_NO_STORE_HEADER_NAMES = {name.lower().encode("ascii") for name in _NO_STORE_HEADERS}


class _MalformedContentLength(ValueError):
    pass


class _RequestBodyTooLarge(ValueError):
    pass


class _ClientDisconnected(ValueError):
    pass


def _request_origin(request: Request) -> str | None:
    # Uvicorn/ProxyHeaders must only rewrite scheme and host for explicitly
    # trusted proxies. This Module deliberately does not trust forwarded
    # headers by itself.
    return canonical_http_origin(str(request.base_url))


def enforce_trusted_auth_origin(
    request: Request,
    *,
    settings: SecuritySettings,
) -> None:
    """Reject browser auth actions that are neither same-origin nor trusted CORS."""

    supplied_origin = request.headers.get("origin")
    supplied_referer = request.headers.get("referer")
    candidate = canonical_http_origin(supplied_origin)
    if supplied_origin is None:
        candidate = canonical_http_origin(
            supplied_referer,
            allow_resource_url=True,
        )

    if candidate is None:
        fetch_site = (request.headers.get("sec-fetch-site") or "").strip().casefold()
        if (
            supplied_origin is not None
            or supplied_referer is not None
            or fetch_site in {"cross-site", "same-site"}
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="不受信任的认证请求来源",
            )
        # Non-browser clients may omit Origin, Referer and Fetch Metadata.
        return

    if candidate == _request_origin(request):
        return

    trusted_cross_origins: set[str] = set()
    if settings.cors_allow_credentials:
        trusted_cross_origins = {
            normalized
            for value in settings.cors_origins
            if (normalized := canonical_http_origin(value)) is not None
        }
    if candidate in trusted_cross_origins:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="不受信任的认证请求来源",
    )


def _is_auth_path(path: str) -> bool:
    return path == "/auth" or path.startswith(_AUTH_PATH_PREFIX)


def _normalized_path(scope: Scope) -> str:
    path = str(scope.get("path", "")) or "/"
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _has_json_content_type(scope: Scope) -> bool:
    raw_value = None
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-type":
            raw_value = value.decode("latin-1")
            break
    if raw_value is None:
        return False
    media_type = raw_value.split(";", 1)[0].strip().casefold()
    if media_type == "application/json":
        return True
    type_name, separator, subtype = media_type.partition("/")
    return type_name == "application" and separator == "/" and subtype.endswith("+json")


def _content_length(scope: Scope) -> int | None:
    values: list[str] = []
    for name, raw_value in scope.get("headers", []):
        if name.lower() != b"content-length":
            continue
        values.extend(
            item.strip()
            for item in raw_value.decode("latin-1").split(",")
            if item.strip()
        )
    if not values:
        return None
    if any(not value.isascii() or not value.isdecimal() for value in values):
        raise _MalformedContentLength
    lengths = {int(value) for value in values}
    if len(lengths) != 1:
        raise _MalformedContentLength
    return lengths.pop()


async def _bounded_body(receive: Receive, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        message_type = message["type"]
        if message_type == "http.disconnect":
            raise _ClientDisconnected
        if message_type != "http.request":
            continue
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise _RequestBodyTooLarge
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay_body(body: bytes) -> Receive:
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    return receive


def _with_no_store(send: Send) -> Send:
    additions = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in _NO_STORE_HEADERS.items()
    ]

    async def send_with_no_store(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = [
                (name, value)
                for name, value in message.get("headers", [])
                if name.lower() not in _NO_STORE_HEADER_NAMES
            ]
            message = {**message, "headers": [*headers, *additions]}
        await send(message)

    return send_with_no_store


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    detail: str,
    stage: str,
) -> None:
    permission_denied = status_code == status.HTTP_403_FORBIDDEN
    public = PublicError(
        (
            ErrorCode.PERMISSION_DENIED
            if permission_denied
            else ErrorCode.INVALID_REQUEST
        ),
        detail,
        status_code=status_code,
        category="security" if permission_denied else "auth",
        stage=stage,
    )
    state = scope.get("state")
    request_id = state.get("request_id") if isinstance(state, dict) else None
    response = JSONResponse(
        status_code=status_code,
        content=error_payload(public, request_id),
        headers=_NO_STORE_HEADERS,
    )
    await response(scope, receive, send)


class AuthRequestGuardMiddleware:
    """Reject unsafe auth metadata before Rate Limit and mark auth as no-store."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: SecuritySettings,
        max_body_bytes: int = AUTH_REQUEST_BODY_LIMIT,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.settings = settings
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = _normalized_path(scope)
        if scope["type"] != "http" or not _is_auth_path(path):
            await self.app(scope, receive, send)
            return

        protected_send = _with_no_store(send)
        method = str(scope.get("method", "GET")).upper()
        if method not in _UNSAFE_METHODS:
            await self.app(scope, receive, protected_send)
            return

        request = Request(scope)
        try:
            enforce_trusted_auth_origin(request, settings=self.settings)
        except HTTPException as exc:
            await _reject(
                scope,
                receive,
                protected_send,
                status_code=exc.status_code,
                detail=str(exc.detail),
                stage="auth_origin",
            )
            return

        try:
            declared_length = _content_length(scope)
        except _MalformedContentLength:
            await _reject(
                scope,
                receive,
                protected_send,
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的 Content-Length",
                stage="auth_content_length",
            )
            return
        if declared_length is not None and declared_length > self.max_body_bytes:
            await _reject(
                scope,
                receive,
                protected_send,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="认证请求体过大",
                stage="auth_body_size",
            )
            return
        has_json_content_type = _has_json_content_type(scope)
        if path in _JSON_BODY_PATHS and not has_json_content_type:
            await _reject(
                scope,
                receive,
                protected_send,
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="认证请求必须使用 application/json",
                stage="auth_content_type",
            )
            return

        await self.app(scope, receive, protected_send)


class AuthBodyLimitMiddleware:
    """Bound streamed auth bodies after Rate Limit has consumed host quota."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = AUTH_REQUEST_BODY_LIMIT,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = _normalized_path(scope)
        method = str(scope.get("method", "GET")).upper()
        if (
            scope["type"] != "http"
            or not _is_auth_path(path)
            or method not in _UNSAFE_METHODS
        ):
            await self.app(scope, receive, send)
            return

        try:
            declared_length = _content_length(scope)
        except _MalformedContentLength:
            await _reject(
                scope,
                receive,
                send,
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的 Content-Length",
                stage="auth_content_length",
            )
            return
        if declared_length is not None and declared_length > self.max_body_bytes:
            await _reject(
                scope,
                receive,
                send,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="认证请求体过大",
                stage="auth_body_size",
            )
            return

        has_json_content_type = _has_json_content_type(scope)
        if path in _JSON_BODY_PATHS and not has_json_content_type:
            await _reject(
                scope,
                receive,
                send,
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="认证请求必须使用 application/json",
                stage="auth_content_type",
            )
            return

        try:
            body = await _bounded_body(receive, limit=self.max_body_bytes)
        except _RequestBodyTooLarge:
            await _reject(
                scope,
                receive,
                send,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="认证请求体过大",
                stage="auth_body_size",
            )
            return
        except _ClientDisconnected:
            await _reject(
                scope,
                receive,
                send,
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="认证请求体未完整传输",
                stage="auth_body",
            )
            return

        if body and not has_json_content_type:
            await _reject(
                scope,
                receive,
                send,
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="认证请求必须使用 application/json",
                stage="auth_content_type",
            )
            return

        await self.app(scope, _replay_body(body), send)


__all__ = [
    "AUTH_REQUEST_BODY_LIMIT",
    "AuthBodyLimitMiddleware",
    "AuthRequestGuardMiddleware",
    "enforce_trusted_auth_origin",
]
