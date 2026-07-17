from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth.cleanup import purge_refresh_token_batch
from backend.core.settings import SecuritySettings
from backend.db.models import RefreshToken, User
from backend.infra.database import Base


def test_refresh_ledger_cleanup_waits_until_expiry_plus_retention():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, RefreshToken.__table__],
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 7, 17, tzinfo=UTC)
    settings = SecuritySettings(
        _env_file=None,
        JWT_SECRET_KEY="auth-cleanup-test-secret-longer-than-thirty-two-characters",
        AUTH_REFRESH_LEDGER_RETENTION_DAYS=30,
    )
    cutoff = now.replace(tzinfo=None) - timedelta(days=30)

    try:
        with sessions() as db:
            user = User(username="alice", password_hash="unused", role="user")
            db.add(user)
            db.flush()
            rows = (
                RefreshToken(
                    id="refresh_old_active",
                    user_id=user.id,
                    token_hash="a" * 64,
                    expires_at=cutoff - timedelta(seconds=1),
                    created_at=cutoff - timedelta(days=60),
                ),
                RefreshToken(
                    id="refresh_old_revoked",
                    user_id=user.id,
                    token_hash="b" * 64,
                    expires_at=cutoff,
                    revoked_at=cutoff - timedelta(days=20),
                    created_at=cutoff - timedelta(days=60),
                ),
                RefreshToken(
                    id="refresh_recently_expired",
                    user_id=user.id,
                    token_hash="c" * 64,
                    expires_at=cutoff + timedelta(seconds=1),
                    created_at=cutoff - timedelta(days=10),
                ),
                RefreshToken(
                    id="refresh_revoked_but_unexpired",
                    user_id=user.id,
                    token_hash="d" * 64,
                    expires_at=now.replace(tzinfo=None) + timedelta(days=1),
                    revoked_at=cutoff - timedelta(days=20),
                    created_at=cutoff - timedelta(days=60),
                ),
            )
            db.add_all(rows)
            db.commit()

        with sessions() as db:
            assert (
                purge_refresh_token_batch(
                    db,
                    settings=settings,
                    now=now,
                    batch_size=1,
                )
                == 1
            )
        with sessions() as db:
            assert (
                purge_refresh_token_batch(
                    db,
                    settings=settings,
                    now=now,
                    batch_size=1,
                )
                == 1
            )
        with sessions() as db:
            assert (
                purge_refresh_token_batch(
                    db,
                    settings=settings,
                    now=now,
                    batch_size=1,
                )
                == 0
            )

        with sessions() as db:
            remaining = {row.id for row in db.query(RefreshToken).all()}
        assert remaining == {
            "refresh_recently_expired",
            "refresh_revoked_but_unexpired",
        }
    finally:
        Base.metadata.drop_all(
            engine,
            tables=[RefreshToken.__table__, User.__table__],
        )
        engine.dispose()
