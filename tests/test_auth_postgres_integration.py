from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.auth.service import (
    AuthTokenService,
    RefreshTokenRejected,
    RefreshTokenRejection,
)
from backend.core.settings import SecuritySettings
from backend.db.models import RefreshToken, User


def _postgres_url() -> str:
    value = os.getenv("AUTH_POSTGRES_TEST_URL", "").strip()
    if not value:
        pytest.skip("AUTH_POSTGRES_TEST_URL is not configured")
    return value


def test_refresh_replay_revokes_replacement_across_two_service_instances():
    engine = create_engine(_postgres_url(), pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = SecuritySettings(
        _env_file=None,
        JWT_SECRET_KEY="postgres-auth-test-secret-longer-than-thirty-two-characters",
        JWT_REFRESH_EXPIRE_DAYS=1,
    )
    first_service = AuthTokenService(settings=settings)
    second_service = AuthTokenService(settings=settings)
    username = f"auth_pg_{uuid4().hex}"

    try:
        with sessions() as db:
            user = User(username=username, password_hash="unused", role="user")
            db.add(user)
            grant = first_service.issue(db, user)
            user_id = user.id

        barrier = Barrier(2)

        def rotate(service: AuthTokenService) -> str:
            with sessions() as db:
                barrier.wait(timeout=5)
                try:
                    service.rotate(db, grant.refresh_token)
                except RefreshTokenRejected as exc:
                    return exc.reason.value
                return "success"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(rotate, (first_service, second_service)))

        assert sorted(results) == [RefreshTokenRejection.REPLAY.value, "success"]
        with sessions() as db:
            active = (
                db.query(RefreshToken)
                .filter(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .count()
            )
        assert active == 0
    finally:
        with sessions() as db:
            db.query(User).filter(User.username == username).delete(
                synchronize_session=False
            )
            db.commit()
        engine.dispose()


def test_rotate_and_logout_all_serialize_on_user_row_without_surviving_token():
    engine = create_engine(_postgres_url(), pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    settings = SecuritySettings(
        _env_file=None,
        JWT_SECRET_KEY="postgres-auth-test-secret-longer-than-thirty-two-characters",
        JWT_REFRESH_EXPIRE_DAYS=1,
    )
    rotate_service = AuthTokenService(settings=settings)
    logout_service = AuthTokenService(settings=settings)
    username = f"auth_pg_{uuid4().hex}"

    try:
        with sessions() as db:
            user = User(username=username, password_hash="unused", role="user")
            db.add(user)
            grant = rotate_service.issue(db, user)
            user_id = user.id

        barrier = Barrier(2)

        def rotate() -> str:
            with sessions() as db:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                barrier.wait(timeout=5)
                try:
                    rotate_service.rotate(db, grant.refresh_token)
                except RefreshTokenRejected as exc:
                    return exc.reason.value
                return "success"

        def logout_all() -> int:
            with sessions() as db:
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                barrier.wait(timeout=5)
                return logout_service.logout_all(db, user_id=user_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            rotate_future = executor.submit(rotate)
            logout_future = executor.submit(logout_all)
            rotate_result = rotate_future.result(timeout=10)
            revoked_count = logout_future.result(timeout=10)

        assert rotate_result in {RefreshTokenRejection.REPLAY.value, "success"}
        assert revoked_count == 1
        with sessions() as db:
            active = (
                db.query(RefreshToken)
                .filter(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .count()
            )
        assert active == 0
    finally:
        with sessions() as db:
            db.query(User).filter(User.username == username).delete(
                synchronize_session=False
            )
            db.commit()
        engine.dispose()
