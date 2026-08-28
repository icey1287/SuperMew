from __future__ import annotations

import hashlib
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy.orm import Session
from starlette.responses import Response

from backend.auth.access import create_access_token
from backend.core.settings import SecuritySettings, get_settings
from backend.db.models import RefreshToken, User


_OPAQUE_REFRESH_TOKEN = re.compile(r"[A-Za-z0-9_-]{64,256}\Z")
_COOKIE_PATH = "/auth"
_LOCK_STRIPES = 64


class RefreshTokenRejection(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"
    REPLAY = "replay"


class RefreshTokenRejected(RuntimeError):
    def __init__(self, reason: RefreshTokenRejection):
        super().__init__(f"refresh token rejected: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TokenGrant:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    username: str
    role: str


def _clock() -> datetime:
    return datetime.now(UTC)


def _token_factory() -> str:
    return secrets.token_urlsafe(48)


def _db_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


class AuthTokenService:
    """Own the access/refresh lifecycle and its transactional invariants."""

    def __init__(
        self,
        *,
        settings: SecuritySettings | None = None,
        clock: Callable[[], datetime] = _clock,
        token_factory: Callable[[], str] = _token_factory,
    ) -> None:
        self.settings = settings or get_settings().security
        self._clock = clock
        self._token_factory = token_factory
        self._locks = tuple(threading.Lock() for _ in range(_LOCK_STRIPES))

    @property
    def cookie_name(self) -> str:
        return self.settings.refresh_cookie_name

    @staticmethod
    def hash_refresh_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def issue(self, db: Session, user: User) -> TokenGrant:
        now = _db_utc(self._clock())
        raw_token = self._new_refresh_token()
        try:
            db.flush()
            locked_user = self._lock_user(db, user.id)
            if locked_user is None:
                raise RuntimeError("cannot issue a refresh token for a missing user")
            db.add(
                RefreshToken(
                    id=f"refresh_{uuid4().hex}",
                    user_id=locked_user.id,
                    token_hash=self.hash_refresh_token(raw_token),
                    expires_at=now
                    + timedelta(days=self.settings.refresh_token_expire_days),
                    created_at=now,
                )
            )
            access_token = create_access_token(
                locked_user.username,
                locked_user.role,
                settings=self.settings,
                now=now.replace(tzinfo=UTC),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return TokenGrant(
            access_token=access_token,
            refresh_token=raw_token,
            username=locked_user.username,
            role=locked_user.role,
        )

    def rotate(self, db: Session, raw_token: str | None) -> TokenGrant:
        token_hash = self._validated_hash(raw_token)
        with self._lock_for(token_hash):
            user_id = self._token_user_id(db, token_hash)
            if user_id is None:
                db.rollback()
                raise RefreshTokenRejected(RefreshTokenRejection.INVALID)
            user = self._lock_user(db, user_id)
            stored = self._lock_token(db, token_hash=token_hash, user_id=user_id)
            if user is None or stored is None:
                db.rollback()
                raise RefreshTokenRejected(RefreshTokenRejection.INVALID)
            now = _db_utc(self._clock())
            if stored.expires_at <= now:
                db.rollback()
                raise RefreshTokenRejected(RefreshTokenRejection.EXPIRED)
            if stored.revoked_at is not None:
                self._revoke_active_for_user(db, user.id, now)
                db.commit()
                raise RefreshTokenRejected(RefreshTokenRejection.REPLAY)

            replacement = self._new_refresh_token()
            stored.revoked_at = now
            db.add(
                RefreshToken(
                    id=f"refresh_{uuid4().hex}",
                    user_id=user.id,
                    token_hash=self.hash_refresh_token(replacement),
                    expires_at=now
                    + timedelta(days=self.settings.refresh_token_expire_days),
                    created_at=now,
                )
            )
            access_token = create_access_token(
                user.username,
                user.role,
                settings=self.settings,
                now=now.replace(tzinfo=UTC),
            )
            try:
                db.commit()
            except BaseException:
                db.rollback()
                raise
            return TokenGrant(
                access_token=access_token,
                refresh_token=replacement,
                username=user.username,
                role=user.role,
            )

    def logout(self, db: Session, raw_token: str | None) -> int:
        try:
            token_hash = self._validated_hash(raw_token)
        except RefreshTokenRejected:
            return 0
        with self._lock_for(token_hash):
            user_id = self._token_user_id(db, token_hash)
            if user_id is None:
                db.rollback()
                return 0
            user = self._lock_user(db, user_id)
            stored = self._lock_token(db, token_hash=token_hash, user_id=user_id)
            if user is None or stored is None or stored.revoked_at is not None:
                db.rollback()
                return 0
            now = _db_utc(self._clock())
            if stored.expires_at <= now:
                db.rollback()
                return 0
            stored.revoked_at = now
            try:
                db.commit()
            except BaseException:
                db.rollback()
                raise
            return 1

    def logout_all(self, db: Session, *, user_id: int) -> int:
        try:
            if self._lock_user(db, user_id) is None:
                db.rollback()
                return 0
            now = _db_utc(self._clock())
            revoked = self._revoke_active_for_user(db, user_id, now)
            db.commit()
            return revoked
        except BaseException:
            db.rollback()
            raise

    def set_refresh_cookie(self, response: Response, raw_token: str) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=raw_token,
            max_age=self.settings.refresh_token_expire_days * 24 * 60 * 60,
            path=_COOKIE_PATH,
            secure=self.settings.refresh_cookie_secure,
            httponly=True,
            samesite=self.settings.refresh_cookie_samesite,
        )

    def clear_refresh_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            path=_COOKIE_PATH,
            secure=self.settings.refresh_cookie_secure,
            httponly=True,
            samesite=self.settings.refresh_cookie_samesite,
        )

    def _new_refresh_token(self) -> str:
        value = self._token_factory()
        if _OPAQUE_REFRESH_TOKEN.fullmatch(value) is None:
            raise RuntimeError("refresh token factory returned an unsafe token")
        return value

    def _validated_hash(self, raw_token: str | None) -> str:
        if raw_token is None:
            raise RefreshTokenRejected(RefreshTokenRejection.MISSING)
        if _OPAQUE_REFRESH_TOKEN.fullmatch(raw_token) is None:
            raise RefreshTokenRejected(RefreshTokenRejection.INVALID)
        return self.hash_refresh_token(raw_token)

    def _lock_for(self, token_hash: str) -> threading.Lock:
        return self._locks[int(token_hash[:8], 16) % len(self._locks)]

    @staticmethod
    def _token_user_id(db: Session, token_hash: str) -> int | None:
        row = (
            db.query(RefreshToken.user_id)
            .filter(RefreshToken.token_hash == token_hash)
            .first()
        )
        return None if row is None else int(row[0])

    @staticmethod
    def _lock_user(db: Session, user_id: int | None) -> User | None:
        if user_id is None:
            return None
        return db.query(User).filter(User.id == user_id).with_for_update().one_or_none()

    @staticmethod
    def _lock_token(
        db: Session,
        *,
        token_hash: str,
        user_id: int,
    ) -> RefreshToken | None:
        return (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash == token_hash,
                RefreshToken.user_id == user_id,
            )
            .with_for_update()
            .one_or_none()
        )

    @staticmethod
    def _revoke_active_for_user(db: Session, user_id: int, now: datetime) -> int:
        return int(
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .update(
                {RefreshToken.revoked_at: now},
                synchronize_session=False,
            )
        )


token_service = AuthTokenService()


__all__ = [
    "AuthTokenService",
    "RefreshTokenRejected",
    "RefreshTokenRejection",
    "TokenGrant",
    "token_service",
]
