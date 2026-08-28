from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.core.errors import install_exception_handlers
from backend.rate_limits.adapters import InMemoryRateLimitAdapter
from backend.rate_limits.auth import (
    AuthRateLimiter,
    enforce_auth_username_rate_limit,
)
from backend.rate_limits.contracts import (
    RateLimitCheck,
    RateLimitDecision,
    RateLimitUnavailable,
)
from backend.rate_limits.limiter import RateLimiter


@dataclass
class StubLimiter:
    decision: RateLimitDecision | None = None
    error: RateLimitUnavailable | None = None
    checks: list[RateLimitCheck] = field(default_factory=list)

    async def check(self, check: RateLimitCheck) -> RateLimitDecision:
        self.checks.append(check)
        if self.error is not None:
            raise self.error
        return self.decision or RateLimitDecision(
            policy_id="auth-login",
            allowed=True,
            limit=10,
            remaining=9,
            retry_after=0,
            reset=1_700_000_060,
        )


def _app(limiter: AuthRateLimiter | None) -> tuple[FastAPI, list[dict]]:
    app = FastAPI()
    app.state.rate_limiter = limiter
    install_exception_handlers(app)
    handled: list[dict] = []

    @app.post("/auth/login")
    def login(
        payload: dict,
        _: None = Depends(enforce_auth_username_rate_limit),
    ) -> dict:
        handled.append(payload)
        return {"ok": True}

    return app, handled


def test_auth_identity_limit_normalizes_username_and_preserves_cached_body():
    limiter = StubLimiter()
    app, handled = _app(limiter)

    with TestClient(app) as client:
        response = client.post(
            "/auth/login",
            json={"username": "  Ａlice  ", "password": "request-secret"},
        )

    assert response.status_code == 200
    assert handled == [{"username": "  Ａlice  ", "password": "request-secret"}]
    assert len(limiter.checks) == 1
    check = limiter.checks[0]
    assert check.client_identity.endswith("\0username:alice")
    assert check.cost == 2
    assert "request-secret" not in check.client_identity
    assert "request-secret" not in repr(check)


def test_auth_identity_denial_happens_before_sync_password_handler():
    limiter = StubLimiter(
        decision=RateLimitDecision(
            policy_id="auth-login",
            allowed=False,
            limit=10,
            remaining=0,
            retry_after=12,
            reset=1_700_000_012,
        )
    )
    app, handled = _app(limiter)

    with TestClient(app) as client:
        response = client.post(
            "/auth/login",
            json={"username": "alice", "password": "request-secret"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "12"
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert handled == []
    assert "alice" not in response.text
    assert "request-secret" not in response.text


def test_auth_identity_storage_failure_fails_closed_before_handler():
    limiter = StubLimiter(
        error=RateLimitUnavailable(adapter="redis", reason="connection_failed")
    )
    app, handled = _app(limiter)

    with TestClient(app) as client:
        response = client.post(
            "/auth/login",
            json={"username": "alice", "password": "request-secret"},
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["code"] == "RATE_LIMIT_UNAVAILABLE"
    assert handled == []
    assert "redis" not in response.text
    assert "request-secret" not in response.text


def test_auth_identity_dependency_is_disabled_when_app_has_no_limiter():
    app, handled = _app(None)

    with TestClient(app) as client:
        response = client.post(
            "/auth/login",
            json={"username": "alice", "password": "request-secret"},
        )

    assert response.status_code == 200
    assert len(handled) == 1


def test_composite_auth_bucket_is_stricter_than_the_ten_request_ip_bucket():
    limiter = RateLimiter(
        InMemoryRateLimitAdapter(clock=lambda: 100.0),
        identity_hmac_key=b"auth-identity-test-key-at-least-32-bytes",
        key_prefix="test",
    )
    app, handled = _app(limiter)

    with TestClient(app) as client:
        responses = [
            client.post(
                "/auth/login",
                json={"username": "alice", "password": "request-secret"},
            )
            for _ in range(6)
        ]

    assert [response.status_code for response in responses] == [
        200,
        200,
        200,
        200,
        200,
        429,
    ]
    assert len(handled) == 5
