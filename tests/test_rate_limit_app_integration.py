from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.rate_limits.contracts import RateLimitCheck, RateLimitDecision


class DenyingLimiter:
    def __init__(self) -> None:
        self.checks: list[RateLimitCheck] = []

    async def check(self, check: RateLimitCheck) -> RateLimitDecision:
        self.checks.append(check)
        return RateLimitDecision(
            policy_id="auth-login",
            allowed=False,
            limit=10,
            remaining=0,
            retry_after=7,
            reset=1_700_000_007,
        )

    async def close(self) -> None:
        return None


def test_app_middleware_keeps_request_id_cors_and_security_headers_on_429():
    import backend.app as app_module

    limiter = DenyingLimiter()
    settings = SimpleNamespace(
        security=SimpleNamespace(
            cors_origins=["https://frontend.example.test"],
            cors_allow_credentials=True,
            refresh_cookie_name="supermew_refresh",
        )
    )
    with (
        patch.object(app_module, "get_settings", return_value=settings),
        patch.object(app_module, "build_rate_limiter", return_value=limiter),
    ):
        app = app_module.create_app()

    client = TestClient(app)
    try:
        response = client.post(
            "/auth/login",
            headers={
                "Origin": "https://frontend.example.test",
                "X-Request-ID": "req_rate_limit_1",
            },
            json={"username": "alice", "password": "request-secret"},
        )
    finally:
        client.close()

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["request_id"] == "req_rate_limit_1"
    assert response.headers["x-request-id"] == "req_rate_limit_1"
    assert response.headers["access-control-allow-origin"] == (
        "https://frontend.example.test"
    )
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "content-security-policy" not in response.headers
    assert len(limiter.checks) == 1
    assert "request-secret" not in repr(limiter.checks[0])


def test_cross_site_auth_form_is_rejected_before_rate_limit_consumption():
    import backend.app as app_module

    limiter = DenyingLimiter()
    settings = SimpleNamespace(
        security=SimpleNamespace(
            cors_origins=["https://frontend.example.test"],
            cors_allow_credentials=True,
            refresh_cookie_name="supermew_refresh",
        )
    )
    with (
        patch.object(app_module, "get_settings", return_value=settings),
        patch.object(app_module, "build_rate_limiter", return_value=limiter),
    ):
        app = app_module.create_app()

    client = TestClient(app)
    try:
        response = client.post(
            "/auth/login",
            headers={
                "Origin": "https://attacker.example",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Request-ID": "req_cross_site_auth_1",
            },
            content="username=alice&password=request-secret",
        )
    finally:
        client.close()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert response.json()["error"]["stage"] == "auth_origin"
    assert response.json()["error"]["request_id"] == "req_cross_site_auth_1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert limiter.checks == []


def test_auth_401_response_is_explicitly_no_store():
    import backend.app as app_module

    settings = SimpleNamespace(
        security=SimpleNamespace(
            cors_origins=["https://frontend.example.test"],
            cors_allow_credentials=True,
            refresh_cookie_name="supermew_refresh",
        )
    )
    with (
        patch.object(app_module, "get_settings", return_value=settings),
        patch.object(app_module, "build_rate_limiter", return_value=None),
    ):
        app = app_module.create_app()

    client = TestClient(app)
    try:
        response = client.get("/auth/me")
    finally:
        client.close()

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
