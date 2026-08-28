from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import Response

import backend.api.routes.auth as auth_routes
import backend.infra.auth as infra_auth
from backend.auth.access import decode_access_token
from backend.auth.origin import AuthBodyLimitMiddleware, AuthRequestGuardMiddleware
from backend.auth.service import (
    AuthTokenService,
    RefreshTokenRejected,
    RefreshTokenRejection,
    TokenGrant,
)
from backend.core.errors import install_exception_handlers
from backend.core.settings import SecuritySettings
from backend.db.models import RefreshToken, User
from backend.infra.auth import authenticate_user, get_password_hash, verify_password
from backend.infra.database import Base


LEGACY_PASSWORD_HASHES = (
    "$2b$04$abcdefghijklmnopqrstuuHQRMHradWrjjbPcbpK37RVvfSYCXoLy",
    "$bcrypt-sha256$2b,4$abcdefghijklmnopqrstuu$FQ2IbYX6zn7VyXVLeDueHXUwEtfuttq",
    "$bcrypt-sha256$v=2,t=2b,r=4$abcdefghijklmnopqrstuu$"
    "Py7aKyeEZmxD.5u4.QZnUu6X5r6LlMS",
)


def _security_settings(*, secure: bool = False) -> SecuritySettings:
    return SecuritySettings(
        _env_file=None,
        JWT_SECRET_KEY="test-auth-secret-which-is-longer-than-thirty-two-chars",
        JWT_EXPIRE_MINUTES=5,
        JWT_REFRESH_EXPIRE_DAYS=7,
        AUTH_REFRESH_COOKIE_NAME="supermew_refresh",
        AUTH_REFRESH_COOKIE_SECURE=secure,
        AUTH_REFRESH_COOKIE_SAMESITE="lax",
    )


@pytest.fixture
def auth_harness(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, RefreshToken.__table__],
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = _security_settings()
    service = AuthTokenService(settings=settings)

    app = FastAPI()
    app.add_middleware(AuthBodyLimitMiddleware)
    app.add_middleware(AuthRequestGuardMiddleware, settings=settings)
    app.include_router(auth_routes.router)

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[auth_routes.get_db] = override_db
    monkeypatch.setattr(auth_routes, "token_service", service)
    monkeypatch.setattr(
        infra_auth,
        "decode_access_token",
        lambda token: decode_access_token(token, settings=settings),
    )

    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            session_factory=session_factory,
            service=service,
            settings=settings,
        )

    Base.metadata.drop_all(
        engine,
        tables=[RefreshToken.__table__, User.__table__],
    )
    engine.dispose()


def _set_cookie_header(response) -> str:
    header = response.headers.get("set-cookie")
    assert header is not None
    return header.lower()


def _response_refresh_token(response, cookie_name: str) -> str:
    raw_token = response.cookies.get(cookie_name)
    assert raw_token is not None
    return raw_token


def _seed_user(harness, username: str, password_hash: str) -> int:
    with harness.session_factory() as db:
        user = User(username=username, password_hash=password_hash, role="user")
        db.add(user)
        db.commit()
        return user.id


def _issue_tokens(harness, username: str, count: int):
    with harness.session_factory() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash="unused", role="user")
            db.add(user)
        grants = [harness.service.issue(db, user) for _ in range(count)]
        return user.id, grants


@pytest.mark.parametrize("missing_claim", ("exp", "iat", "jti"))
def test_access_token_rejects_missing_lifecycle_claims(missing_claim):
    settings = _security_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": "alice",
        "role": "user",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "access_0123456789abcdef0123456789abcdef",
    }
    del payload[missing_claim]
    token = jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(token, settings=settings)


def test_access_token_rejects_invalid_jti():
    settings = _security_settings()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "alice",
            "role": "user",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": "",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(jwt.InvalidTokenError, match="jti"):
        decode_access_token(token, settings=settings)


def test_token_grant_repr_redacts_both_bearer_tokens():
    grant = TokenGrant(
        access_token="access-secret",
        refresh_token="refresh-secret",
        username="alice",
        role="user",
    )

    rendered = repr(grant)

    assert "access-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "alice" in rendered


def test_register_and_login_set_http_only_hash_only_refresh_cookie(auth_harness):
    client = auth_harness.client
    cookie_name = auth_harness.service.cookie_name

    registered = client.post(
        "/auth/register",
        json={"username": "alice", "password": "correct-password"},
    )

    assert registered.status_code == 200
    assert set(registered.json()) == {
        "access_token",
        "token_type",
        "username",
        "role",
    }
    assert registered.json()["username"] == "alice"
    assert registered.json()["role"] == "user"
    assert registered.headers["cache-control"] == "no-store"
    assert registered.headers["pragma"] == "no-cache"
    registered_raw = _response_refresh_token(registered, cookie_name)
    registered_cookie = _set_cookie_header(registered)
    assert "httponly" in registered_cookie
    assert "path=/auth" in registered_cookie
    assert "samesite=lax" in registered_cookie
    assert "; secure" not in registered_cookie
    assert registered_raw not in registered.text

    with auth_harness.session_factory() as db:
        rows = db.query(RefreshToken).all()
        assert len(rows) == 1
        assert (
            rows[0].token_hash
            == hashlib.sha256(registered_raw.encode("utf-8")).hexdigest()
        )
        assert rows[0].token_hash != registered_raw

    client.cookies.clear()
    logged_in = client.post(
        "/auth/login",
        json={"username": "alice", "password": "correct-password"},
    )

    assert logged_in.status_code == 200
    login_raw = _response_refresh_token(logged_in, cookie_name)
    login_cookie = _set_cookie_header(logged_in)
    assert "httponly" in login_cookie
    assert "path=/auth" in login_cookie
    assert "samesite=lax" in login_cookie
    assert login_raw not in logged_in.text
    assert login_raw != registered_raw

    with auth_harness.session_factory() as db:
        stored_hashes = {row.token_hash for row in db.query(RefreshToken).all()}
    assert registered_raw not in stored_hashes
    assert login_raw not in stored_hashes
    assert hashlib.sha256(login_raw.encode("utf-8")).hexdigest() in stored_hashes


def test_secure_cookie_configuration_is_reflected_in_set_and_clear_headers():
    service = AuthTokenService(settings=_security_settings(secure=True))
    response = Response()
    service.set_refresh_cookie(response, "A" * 64)

    set_header = response.headers["set-cookie"].lower()
    assert "httponly" in set_header
    assert "path=/auth" in set_header
    assert "samesite=lax" in set_header
    assert "; secure" in set_header

    cleared = Response()
    service.clear_refresh_cookie(cleared)
    clear_header = cleared.headers["set-cookie"].lower()
    assert "max-age=0" in clear_header
    assert "path=/auth" in clear_header
    assert "httponly" in clear_header
    assert "; secure" in clear_header


def test_global_http_error_handler_preserves_refresh_cookie_clear(monkeypatch):
    service = AuthTokenService(settings=_security_settings())
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_routes.router)
    app.dependency_overrides[auth_routes.get_db] = lambda: object()
    monkeypatch.setattr(auth_routes, "token_service", service)

    with TestClient(app) as client:
        client.cookies.set(service.cookie_name, "malformed-refresh-token")
        rejected = client.post("/auth/refresh")

    assert rejected.status_code == 401
    clear_header = _set_cookie_header(rejected)
    assert "max-age=0" in clear_header
    assert "path=/auth" in clear_header
    assert "httponly" in clear_header


def test_refresh_rotates_once_and_revokes_the_presented_token(auth_harness):
    registered = auth_harness.client.post(
        "/auth/register",
        json={"username": "alice", "password": "correct-password"},
    )
    original = _response_refresh_token(
        registered,
        auth_harness.service.cookie_name,
    )

    refreshed = auth_harness.client.post("/auth/refresh")

    assert refreshed.status_code == 200
    replacement = _response_refresh_token(
        refreshed,
        auth_harness.service.cookie_name,
    )
    assert replacement != original
    assert refreshed.json()["username"] == "alice"
    assert replacement not in refreshed.text

    with auth_harness.session_factory() as db:
        original_row = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(original)
            )
            .one()
        )
        replacement_row = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(replacement)
            )
            .one()
        )
        assert original_row.revoked_at is not None
        assert replacement_row.revoked_at is None


def test_cookie_backed_auth_rejects_cross_site_origin_before_token_mutation(
    auth_harness,
):
    registered = auth_harness.client.post(
        "/auth/register",
        json={"username": "alice", "password": "correct-password"},
    )
    original = _response_refresh_token(
        registered,
        auth_harness.service.cookie_name,
    )

    rejected_refresh = auth_harness.client.post(
        "/auth/refresh",
        headers={"Origin": "https://attacker.example"},
    )
    rejected_logout = auth_harness.client.post(
        "/auth/logout",
        headers={"Origin": "null"},
    )

    assert rejected_refresh.status_code == 403
    assert rejected_logout.status_code == 403
    with auth_harness.session_factory() as db:
        stored = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(original)
            )
            .one()
        )
        assert stored.revoked_at is None

    allowed = auth_harness.client.post(
        "/auth/refresh",
        headers={"Origin": "http://localhost:3000"},
    )
    assert allowed.status_code == 200
    assert (
        _response_refresh_token(allowed, auth_harness.service.cookie_name) != original
    )


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    (
        ({"username": "x" * 101, "password": "secret"}, 422),
        ({"username": "alice", "password": "x" * 1025}, 422),
        (
            {
                "username": "alice",
                "password": "secret",
                "role": "admin",
                "admin_code": "x" * 257,
            },
            422,
        ),
        ({"username": "\ufb03" * 34, "password": "secret"}, 400),
    ),
)
def test_register_rejects_oversized_auth_fields_without_database_error(
    auth_harness,
    payload,
    expected_status,
):
    response = auth_harness.client.post("/auth/register", json=payload)

    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    with auth_harness.session_factory() as db:
        assert db.query(User).count() == 0


def test_replayed_refresh_revokes_only_the_owning_users_active_tokens(auth_harness):
    alice_id, alice_grants = _issue_tokens(auth_harness, "alice", 2)
    bob_id, _ = _issue_tokens(auth_harness, "bob", 1)

    with auth_harness.session_factory() as db:
        auth_harness.service.rotate(db, alice_grants[0].refresh_token)

    with auth_harness.session_factory() as db:
        with pytest.raises(RefreshTokenRejected) as raised:
            auth_harness.service.rotate(db, alice_grants[0].refresh_token)
    assert raised.value.reason is RefreshTokenRejection.REPLAY

    with auth_harness.session_factory() as db:
        alice_active = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == alice_id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
        bob_active = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == bob_id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
    assert alice_active == 0
    assert bob_active == 1


def test_expired_refresh_is_rejected_without_becoming_replay(auth_harness):
    registered = auth_harness.client.post(
        "/auth/register",
        json={"username": "alice", "password": "correct-password"},
    )
    raw_token = _response_refresh_token(
        registered,
        auth_harness.service.cookie_name,
    )

    with auth_harness.session_factory() as db:
        stored = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(raw_token)
            )
            .one()
        )
        stored.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        db.commit()

    rejected = auth_harness.client.post("/auth/refresh")

    assert rejected.status_code == 401
    clear_header = _set_cookie_header(rejected)
    assert "max-age=0" in clear_header
    assert "path=/auth" in clear_header

    with auth_harness.session_factory() as db:
        stored = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(raw_token)
            )
            .one()
        )
        assert stored.revoked_at is None
        with pytest.raises(RefreshTokenRejected) as raised:
            auth_harness.service.rotate(db, raw_token)
    assert raised.value.reason is RefreshTokenRejection.EXPIRED


def test_expired_revoked_refresh_does_not_revoke_other_active_devices(auth_harness):
    alice_id, grants = _issue_tokens(auth_harness, "alice", 2)
    expired_replayed = grants[0].refresh_token

    with auth_harness.session_factory() as db:
        stored = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(expired_replayed)
            )
            .one()
        )
        stored.revoked_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
        stored.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        db.commit()

    with auth_harness.session_factory() as db:
        with pytest.raises(RefreshTokenRejected) as raised:
            auth_harness.service.rotate(db, expired_replayed)
    assert raised.value.reason is RefreshTokenRejection.EXPIRED

    with auth_harness.session_factory() as db:
        active = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == alice_id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
    assert active == 1


@pytest.mark.parametrize(
    "raw_token",
    ("too-short", "x" * 257, "contains spaces"),
)
def test_logout_ignores_malformed_refresh_cookie_without_hashing_or_querying(
    auth_harness,
    monkeypatch,
    raw_token,
):
    def fail_hash(_: str) -> str:
        raise AssertionError("malformed refresh token must not be hashed")

    monkeypatch.setattr(auth_harness.service, "hash_refresh_token", fail_hash)

    with auth_harness.session_factory() as db:
        assert auth_harness.service.logout(db, raw_token) == 0


def test_logout_is_idempotent_and_clears_the_refresh_cookie(auth_harness):
    registered = auth_harness.client.post(
        "/auth/register",
        json={"username": "alice", "password": "correct-password"},
    )
    raw_token = _response_refresh_token(
        registered,
        auth_harness.service.cookie_name,
    )

    first = auth_harness.client.post("/auth/logout")
    second = auth_harness.client.post("/auth/logout")

    assert first.status_code == 200
    assert first.json() == {"message": "已退出登录", "revoked_count": 1}
    assert "max-age=0" in _set_cookie_header(first)
    assert second.status_code == 200
    assert second.json() == {"message": "已退出登录", "revoked_count": 0}
    assert "max-age=0" in _set_cookie_header(second)

    with auth_harness.session_factory() as db:
        stored = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash
                == auth_harness.service.hash_refresh_token(raw_token)
            )
            .one()
        )
        assert stored.revoked_at is not None


def test_logout_all_revokes_only_the_authenticated_users_tokens(auth_harness):
    registered = auth_harness.client.post(
        "/auth/register",
        json={"username": "alice", "password": "correct-password"},
    )
    access_token = registered.json()["access_token"]

    alice_id, _ = _issue_tokens(auth_harness, "alice", 1)
    bob_id, _ = _issue_tokens(auth_harness, "bob", 1)

    response = auth_harness.client.post(
        "/auth/logout-all",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"message": "已退出所有设备", "revoked_count": 2}
    assert "max-age=0" in _set_cookie_header(response)

    with auth_harness.session_factory() as db:
        alice_active = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == alice_id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
        bob_active = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == bob_id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
    assert alice_active == 0
    assert bob_active == 1


@pytest.mark.parametrize("legacy_hash", LEGACY_PASSWORD_HASHES)
def test_successful_legacy_login_upgrades_password_hash_atomically(
    auth_harness,
    legacy_hash,
):
    _seed_user(auth_harness, "legacy-user", legacy_hash)

    response = auth_harness.client.post(
        "/auth/login",
        json={"username": "legacy-user", "password": "legacy-password"},
    )

    assert response.status_code == 200
    with auth_harness.session_factory() as db:
        user = db.query(User).filter(User.username == "legacy-user").one()
        assert user.password_hash.startswith("pbkdf2_sha256$")
        assert user.password_hash != legacy_hash
        assert verify_password("legacy-password", user.password_hash)
        assert (
            db.query(RefreshToken).filter(RefreshToken.user_id == user.id).count() == 1
        )


def test_failed_refresh_issue_rolls_back_legacy_password_upgrade(auth_harness):
    legacy_hash = LEGACY_PASSWORD_HASHES[0]
    user_id = _seed_user(auth_harness, "legacy-user", legacy_hash)

    with auth_harness.session_factory() as db:
        user = authenticate_user(db, "legacy-user", "legacy-password")
        assert user is not None
        assert user.password_hash.startswith("pbkdf2_sha256$")

        def fail_commit_after_flush():
            db.flush()
            raise RuntimeError("commit failed")

        db.commit = fail_commit_after_flush
        with pytest.raises(RuntimeError, match="commit failed"):
            auth_harness.service.issue(db, user)

    with auth_harness.session_factory() as db:
        stored = db.query(User).filter(User.id == user_id).one()
        assert stored.password_hash == legacy_hash
        assert (
            db.query(RefreshToken).filter(RefreshToken.user_id == user_id).count() == 0
        )


def test_wrong_legacy_password_does_not_migrate_or_issue_refresh(auth_harness):
    legacy_hash = LEGACY_PASSWORD_HASHES[0]
    user_id = _seed_user(auth_harness, "legacy-user", legacy_hash)

    response = auth_harness.client.post(
        "/auth/login",
        json={"username": "legacy-user", "password": "wrong-password"},
    )

    assert response.status_code == 401
    with auth_harness.session_factory() as db:
        user = db.query(User).filter(User.id == user_id).one()
        assert user.password_hash == legacy_hash
        assert (
            db.query(RefreshToken).filter(RefreshToken.user_id == user_id).count() == 0
        )


def test_pbkdf2_login_does_not_rewrite_existing_password_hash(auth_harness):
    existing_hash = get_password_hash("correct-password")
    user_id = _seed_user(auth_harness, "alice", existing_hash)

    response = auth_harness.client.post(
        "/auth/login",
        json={"username": "alice", "password": "correct-password"},
    )

    assert response.status_code == 200
    with auth_harness.session_factory() as db:
        user = db.query(User).filter(User.id == user_id).one()
        assert user.password_hash == existing_hash


def test_concurrent_rotation_grants_once_then_detects_replay(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'auth-concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, RefreshToken.__table__],
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = AuthTokenService(settings=_security_settings())

    with session_factory() as db:
        user = User(username="alice", password_hash="unused", role="user")
        db.add(user)
        original = service.issue(db, user).refresh_token
        user_id = user.id

    barrier = Barrier(2)

    def rotate_once():
        with session_factory() as db:
            barrier.wait()
            try:
                grant = service.rotate(db, original)
            except RefreshTokenRejected as exc:
                return "rejected", exc.reason
            return "granted", grant.refresh_token

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rotate_once) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(result[0] for result in results) == ["granted", "rejected"]
    rejected = next(result for result in results if result[0] == "rejected")
    assert rejected[1] is RefreshTokenRejection.REPLAY

    with session_factory() as db:
        active = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .count()
        )
        assert active == 0

    Base.metadata.drop_all(
        engine,
        tables=[RefreshToken.__table__, User.__table__],
    )
    engine.dispose()
