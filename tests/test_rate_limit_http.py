from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable

from starlette.types import Message, Receive, Scope, Send

from backend.rate_limits.contracts import (
    RateLimitDecision,
    RateLimitUnavailable,
)
from backend.rate_limits.contracts import RateLimitCheck
from backend.rate_limits.http import RateLimitMiddleware


def _decision(
    *,
    allowed: bool = True,
    policy_id: str = "api-general",
    limit: int = 120,
    remaining: int = 119,
    retry_after: int = 0,
    reset: int = 1_700_000_060,
) -> RateLimitDecision:
    return RateLimitDecision(
        policy_id=policy_id,
        allowed=allowed,
        limit=limit,
        remaining=remaining,
        retry_after=retry_after,
        reset=reset,
    )


class StubLimiter:
    def __init__(
        self,
        decision: RateLimitDecision | None = None,
        error: RateLimitUnavailable | None = None,
    ) -> None:
        self.decision = decision or _decision()
        self.error = error
        self.checks: list[RateLimitCheck] = []

    def check(self, check: RateLimitCheck) -> Awaitable[RateLimitDecision]:
        async def run() -> RateLimitDecision:
            self.checks.append(check)
            if self.error is not None:
                raise self.error
            return self.decision

        return run()


class BodyReadingApp:
    def __init__(self) -> None:
        self.calls = 0
        self.received: list[Message] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        if scope["type"] == "http":
            self.received.append(await receive())
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"x-app", b"called")],
            }
        )
        await send({"type": "http.response.body", "body": b""})


async def _invoke(
    middleware: RateLimitMiddleware,
    *,
    method: str,
    path: str,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    client: tuple[str, int] | None = ("203.0.113.7", 43123),
    body: bytes = b"request-body-secret",
    request_id: str | None = None,
) -> tuple[list[Message], int]:
    receive_calls = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    async def send(message: Message) -> None:
        messages.append(message)

    state = {"request_id": request_id} if request_id else {}
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": client,
        "server": ("testserver", 80),
        "state": state,
    }
    await middleware(scope, receive, send)
    return messages, receive_calls


def _status(messages: list[Message]) -> int:
    return next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )


def _headers(messages: list[Message]) -> dict[str, str]:
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }


def _json_body(messages: list[Message]) -> dict:
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


class RateLimitHttpIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_selection_uses_only_the_required_source(self):
        app = BodyReadingApp()
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(
            app,
            limiter=limiter,
            bearer_subject_resolver=lambda _token: "alice",
        )
        bearer = b"authorization", b"Bearer access-secret"
        cookie = b"cookie", b"other=x; supermew_refresh=refresh-secret"
        cases = (
            ("/auth/login", (bearer, cookie), "host:203.0.113.7"),
            ("/auth/login/", (bearer, cookie), "host:203.0.113.7"),
            ("/auth/register", (bearer, cookie), "host:203.0.113.7"),
            ("/auth/refresh", (bearer, cookie), "host:203.0.113.7"),
            ("/auth/logout", (bearer, cookie), "host:203.0.113.7"),
            ("/auth/logout-all", (bearer, cookie), "subject:alice"),
            ("/v1/threads/thread_1/runs", (bearer, cookie), "subject:alice"),
            ("/documents/upload/async", (), "host:203.0.113.7"),
        )

        for path, headers, expected in cases:
            with self.subTest(path=path):
                await _invoke(middleware, method="POST", path=path, headers=headers)
                self.assertEqual(expected, limiter.checks[-1].client_identity)

    async def test_rotated_credentials_keep_the_same_stable_quota_identity(self):
        limiter = StubLimiter()
        subjects = {
            "access-before-refresh": "alice",
            "access-after-refresh": "alice",
        }
        middleware = RateLimitMiddleware(
            BodyReadingApp(),
            limiter=limiter,
            bearer_subject_resolver=subjects.get,
        )

        for token in subjects:
            await _invoke(
                middleware,
                method="POST",
                path="/v1/threads/thread_1/runs",
                headers=((b"authorization", f"Bearer {token}".encode("ascii")),),
            )
        for refresh in ("refresh-before", "refresh-after"):
            await _invoke(
                middleware,
                method="POST",
                path="/auth/refresh",
                headers=((b"cookie", f"supermew_refresh={refresh}".encode("ascii")),),
            )

        self.assertEqual(
            ["subject:alice", "subject:alice"],
            [check.client_identity for check in limiter.checks[:2]],
        )
        self.assertEqual(
            ["host:203.0.113.7", "host:203.0.113.7"],
            [check.client_identity for check in limiter.checks[2:]],
        )
        serialized = repr(limiter.checks)
        self.assertNotIn("access-before-refresh", serialized)
        self.assertNotIn("refresh-before", serialized)

    async def test_spoofed_forwarded_for_is_not_used_as_client_host(self):
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(BodyReadingApp(), limiter=limiter)

        await _invoke(
            middleware,
            method="POST",
            path="/auth/login",
            headers=((b"x-forwarded-for", b"198.51.100.99"),),
        )

        self.assertEqual("host:203.0.113.7", limiter.checks[0].client_identity)

    async def test_malformed_or_oversized_credentials_fall_back_to_host(self):
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(
            BodyReadingApp(),
            limiter=limiter,
            bearer_subject_resolver=lambda _token: None,
        )

        await _invoke(
            middleware,
            method="POST",
            path="/v1/threads/thread_1/runs",
            headers=((b"authorization", b"Bearer token with spaces"),),
        )
        await _invoke(
            middleware,
            method="POST",
            path="/auth/refresh",
            headers=((b"cookie", b"supermew_refresh=" + (b"x" * 4_097)),),
        )

        self.assertEqual("host:203.0.113.7", limiter.checks[0].client_identity)
        self.assertEqual("host:203.0.113.7", limiter.checks[1].client_identity)


class RateLimitHttpSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_preflight_health_docs_and_static_paths_are_skipped(self):
        app = BodyReadingApp()
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(app, limiter=limiter)
        cases = (
            ("OPTIONS", "/auth/login"),
            ("GET", "/health/live"),
            ("GET", "/health/ready"),
            ("GET", "/v1/health/ready"),
            ("GET", "/v1/health"),
            ("GET", "/docs"),
            ("GET", "/openapi.json"),
            ("GET", "/"),
            ("GET", "/index.html"),
            ("GET", "/static/app.js"),
            ("GET", "/assets/app.css"),
        )

        for method, path in cases:
            with self.subTest(path=path):
                messages, _ = await _invoke(middleware, method=method, path=path)
                self.assertEqual(204, _status(messages))

        self.assertEqual(len(cases), app.calls)
        self.assertEqual([], limiter.checks)

    async def test_unknown_dynamic_paths_default_to_general_limiting(self):
        app = BodyReadingApp()
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(app, limiter=limiter)

        await _invoke(middleware, method="POST", path="/authentication/login")
        await _invoke(middleware, method="POST", path="/chatty")
        await _invoke(middleware, method="GET", path="/future-list")
        await _invoke(middleware, method="DELETE", path="/future-list/item-1")
        await _invoke(middleware, method="GET", path="/future-api")

        self.assertEqual(5, len(limiter.checks))
        self.assertEqual(
            [
                "/authentication/login",
                "/chatty",
                "/future-list",
                "/future-list/item-1",
                "/future-api",
            ],
            [check.path for check in limiter.checks],
        )


class RateLimitHttpResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_oversized_paths_fail_closed_without_calling_limiter(self):
        app = BodyReadingApp()
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(app, limiter=limiter)

        invalid_messages, invalid_receive_calls = await _invoke(
            middleware,
            method="GET",
            path="/docs/\x00log-injection",
            request_id="req_invalid_path",
        )
        oversized_messages, oversized_receive_calls = await _invoke(
            middleware,
            method="GET",
            path=f"/future-api/{'x' * 2_048}",
            request_id="req_oversized_path",
        )

        self.assertEqual(400, _status(invalid_messages))
        self.assertEqual(414, _status(oversized_messages))
        self.assertEqual(
            "INVALID_REQUEST", _json_body(invalid_messages)["error"]["code"]
        )
        self.assertEqual(
            "req_oversized_path",
            _json_body(oversized_messages)["error"]["request_id"],
        )
        self.assertEqual(0, invalid_receive_calls)
        self.assertEqual(0, oversized_receive_calls)
        self.assertEqual(0, app.calls)
        self.assertEqual([], limiter.checks)

    async def test_relative_path_fails_closed_instead_of_becoming_static_root(self):
        app = BodyReadingApp()
        limiter = StubLimiter()
        middleware = RateLimitMiddleware(app, limiter=limiter)

        messages, receive_calls = await _invoke(
            middleware,
            method="GET",
            path="relative-path",
        )

        self.assertEqual(400, _status(messages))
        self.assertEqual(0, receive_calls)
        self.assertEqual(0, app.calls)
        self.assertEqual([], limiter.checks)

    async def test_denied_request_returns_uniform_429_without_reading_body(self):
        app = BodyReadingApp()
        limiter = StubLimiter(
            _decision(
                allowed=False,
                policy_id="auth-login",
                limit=10,
                remaining=0,
                retry_after=12,
                reset=1_700_000_012,
            )
        )
        middleware = RateLimitMiddleware(app, limiter=limiter)

        messages, receive_calls = await _invoke(
            middleware,
            method="POST",
            path="/auth/login",
            headers=((b"authorization", b"Bearer response-secret"),),
            request_id="req_safe_1",
        )

        self.assertEqual(429, _status(messages))
        self.assertEqual(0, app.calls)
        self.assertEqual(0, receive_calls)
        headers = _headers(messages)
        self.assertEqual("12", headers["retry-after"])
        self.assertEqual("10", headers["ratelimit-limit"])
        self.assertEqual("0", headers["ratelimit-remaining"])
        self.assertEqual("1700000012", headers["ratelimit-reset"])
        payload = _json_body(messages)
        self.assertEqual("RATE_LIMITED", payload["error"]["code"])
        self.assertEqual("req_safe_1", payload["error"]["request_id"])
        self.assertEqual(12.0, payload["error"]["retry_after"])
        self.assertNotIn("response-secret", json.dumps(payload))

    async def test_unavailable_fails_closed_with_redacted_503(self):
        app = BodyReadingApp()
        limiter = StubLimiter(
            error=RateLimitUnavailable(
                adapter="redis",
                reason="connection_failed",
            )
        )
        middleware = RateLimitMiddleware(app, limiter=limiter)

        messages, receive_calls = await _invoke(
            middleware,
            method="POST",
            path="/auth/refresh",
            headers=((b"cookie", b"supermew_refresh=refresh-response-secret"),),
        )

        self.assertEqual(503, _status(messages))
        self.assertEqual(0, app.calls)
        self.assertEqual(0, receive_calls)
        self.assertEqual("1", _headers(messages)["retry-after"])
        payload = _json_body(messages)
        self.assertEqual("RATE_LIMIT_UNAVAILABLE", payload["error"]["code"])
        serialized = json.dumps(payload)
        self.assertNotIn("refresh-response-secret", serialized)
        self.assertNotIn("redis", serialized)
        self.assertNotIn("connection_failed", serialized)

    async def test_allowed_request_gets_rate_limit_headers_and_body_is_only_read_downstream(
        self,
    ):
        app = BodyReadingApp()
        limiter = StubLimiter(_decision(limit=30, remaining=29, reset=1_700_000_060))
        middleware = RateLimitMiddleware(app, limiter=limiter)

        messages, receive_calls = await _invoke(
            middleware,
            method="POST",
            path="/v1/threads/thread_1/runs",
        )

        self.assertEqual(204, _status(messages))
        self.assertEqual(1, app.calls)
        self.assertEqual(1, receive_calls)
        self.assertEqual(b"request-body-secret", app.received[0]["body"])
        headers = _headers(messages)
        self.assertEqual("called", headers["x-app"])
        self.assertEqual("30", headers["ratelimit-limit"])
        self.assertEqual("29", headers["ratelimit-remaining"])
        self.assertEqual("1700000060", headers["ratelimit-reset"])
        self.assertNotIn("retry-after", headers)


if __name__ == "__main__":
    unittest.main()
