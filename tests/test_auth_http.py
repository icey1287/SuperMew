from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.auth.origin import (
    AUTH_REQUEST_BODY_LIMIT,
    AuthBodyLimitMiddleware,
    AuthRequestGuardMiddleware,
)
from backend.core.settings import SecuritySettings


class RecordingApp:
    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls = 0
        self.bodies: list[bytes] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        message = await receive()
        self.bodies.append(message.get("body", b""))
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})


class RecordingRateLimit:
    def __init__(self, app: ASGIApp, events: list[str]) -> None:
        self.app = app
        self.events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.events.append("host_quota_consumed")
        await self.app(scope, receive, send)


def _settings(*, credentials: bool) -> SecuritySettings:
    return SecuritySettings(
        _env_file=None,
        JWT_SECRET_KEY="auth-http-test-secret-longer-than-thirty-two-characters",
        CORS_ORIGINS="https://frontend.example.test",
        CORS_ALLOW_CREDENTIALS=credentials,
    )


async def _invoke(
    middleware: ASGIApp,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    messages: Sequence[Message] | None = None,
    path: str = "/auth/login",
    events: list[str] | None = None,
    request_id: str = "req_auth_guard_1",
) -> tuple[list[Message], int]:
    incoming = list(
        messages
        or [
            {
                "type": "http.request",
                "body": b'{"username":"alice","password":"secret"}',
                "more_body": False,
            }
        ]
    )
    receive_calls = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if events is not None:
            events.append("body_read")
        if incoming:
            return incoming.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    header_list = list(headers)
    if not any(name.lower() == b"content-type" for name, _ in header_list):
        header_list.append((b"content-type", b"application/json"))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), *header_list],
        "client": ("203.0.113.7", 43123),
        "server": ("testserver", 80),
        "state": {"request_id": request_id},
    }
    await middleware(scope, receive, send)
    return sent, receive_calls


def _protected(app: ASGIApp, *, credentials: bool) -> ASGIApp:
    body_limit = AuthBodyLimitMiddleware(app)
    return AuthRequestGuardMiddleware(
        body_limit,
        settings=_settings(credentials=credentials),
    )


def _status(messages: Sequence[Message]) -> int:
    return next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )


def _headers(messages: Sequence[Message]) -> dict[str, str]:
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }


def _body(messages: Sequence[Message]) -> dict:
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_same_origin_is_allowed_when_cross_origin_credentials_are_disabled():
    app = RecordingApp()
    middleware = _protected(app, credentials=False)

    messages, _ = await _invoke(
        middleware,
        headers=((b"origin", b"http://testserver"),),
    )

    assert _status(messages) == 200
    assert app.calls == 1


@pytest.mark.asyncio
async def test_cross_origin_requires_both_credentials_and_explicit_allowlist():
    denied_app = RecordingApp()
    denied = _protected(denied_app, credentials=False)
    allowed_app = RecordingApp()
    allowed = _protected(allowed_app, credentials=True)

    denied_messages, denied_reads = await _invoke(
        denied,
        headers=((b"origin", b"https://frontend.example.test"),),
    )
    allowed_messages, _ = await _invoke(
        allowed,
        headers=((b"origin", b"https://frontend.example.test"),),
    )

    assert _status(denied_messages) == 403
    assert denied_reads == 0
    assert denied_app.calls == 0
    assert _status(allowed_messages) == 200
    assert allowed_app.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    (
        ((b"origin", b"https://evil.example"),),
        ((b"origin", b"null"),),
        ((b"referer", b"https://evil.example/form"),),
        ((b"sec-fetch-site", b"cross-site"),),
    ),
)
async def test_evil_null_and_cross_site_browser_requests_are_rejected(headers):
    app = RecordingApp()
    middleware = _protected(app, credentials=True)

    messages, receive_calls = await _invoke(middleware, headers=headers)

    assert _status(messages) == 403
    assert receive_calls == 0
    assert app.calls == 0
    payload = _body(messages)["error"]
    assert payload["code"] == "PERMISSION_DENIED"
    assert payload["stage"] == "auth_origin"
    assert payload["request_id"] == "req_auth_guard_1"


@pytest.mark.asyncio
async def test_oversized_content_length_fails_before_reading_or_downstream_limit():
    app = RecordingApp()
    middleware = _protected(app, credentials=True)

    messages, receive_calls = await _invoke(
        middleware,
        headers=(
            (b"origin", b"http://testserver"),
            (b"content-length", str(AUTH_REQUEST_BODY_LIMIT + 1).encode("ascii")),
        ),
    )

    assert _status(messages) == 413
    assert receive_calls == 0
    assert app.calls == 0
    assert _body(messages)["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_chunked_body_consumes_host_quota_before_streaming_cap_returns_413():
    app = RecordingApp()
    events: list[str] = []
    body_limit = AuthBodyLimitMiddleware(app)
    rate_limit = RecordingRateLimit(body_limit, events)
    middleware = AuthRequestGuardMiddleware(
        rate_limit,
        settings=_settings(credentials=True),
    )
    first = b"a" * (AUTH_REQUEST_BODY_LIMIT // 2)
    second = b"b" * (AUTH_REQUEST_BODY_LIMIT // 2 + 1)

    messages, receive_calls = await _invoke(
        middleware,
        headers=((b"origin", b"http://testserver"),),
        messages=(
            {"type": "http.request", "body": first, "more_body": True},
            {"type": "http.request", "body": second, "more_body": False},
        ),
        events=events,
    )

    assert _status(messages) == 413
    assert receive_calls == 2
    assert events == ["host_quota_consumed", "body_read", "body_read"]
    assert app.calls == 0
    assert _body(messages)["error"] == {
        "code": "INVALID_REQUEST",
        "message": "认证请求体过大",
        "retryable": False,
        "category": "auth",
        "stage": "auth_body_size",
        "provider": None,
        "retry_after": None,
        "request_id": "req_auth_guard_1",
        "details": {},
    }


@pytest.mark.asyncio
async def test_form_encoded_login_is_rejected_before_reading_or_downstream_limit():
    app = RecordingApp()
    middleware = _protected(app, credentials=True)

    messages, receive_calls = await _invoke(
        middleware,
        headers=(
            (b"origin", b"http://testserver"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ),
    )

    assert _status(messages) == 415
    assert receive_calls == 0
    assert app.calls == 0
    assert _body(messages)["error"]["stage"] == "auth_content_type"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (200, 401, 429))
async def test_auth_responses_are_no_store_and_allowed_body_is_replayed(status_code):
    app = RecordingApp(status_code=status_code)
    middleware = _protected(app, credentials=True)
    body = b'{"username":"alice"}'

    messages, _ = await _invoke(
        middleware,
        headers=((b"origin", b"http://testserver"),),
        messages=({"type": "http.request", "body": body, "more_body": False},),
    )

    assert _status(messages) == status_code
    assert app.bodies == [body]
    response_headers = _headers(messages)
    assert response_headers["cache-control"] == "no-store"
    assert response_headers["pragma"] == "no-cache"
