from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
from uuid import uuid4

import jwt

from backend.core.settings import SecuritySettings, get_settings


_ACCESS_JTI = re.compile(r"access_[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    username: str
    role: str


def _utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        return resolved.replace(tzinfo=UTC)
    return resolved.astimezone(UTC)


def create_access_token(
    username: str,
    role: str,
    *,
    settings: SecuritySettings | None = None,
    now: datetime | None = None,
) -> str:
    security = settings or get_settings().security
    issued_at = _utc(now)
    payload = {
        "sub": username,
        "role": role,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=security.access_token_expire_minutes),
        "jti": f"access_{uuid4().hex}",
    }
    return jwt.encode(
        payload,
        security.jwt_secret_key.get_secret_value(),
        algorithm=security.jwt_algorithm,
    )


def decode_access_token(
    token: str,
    *,
    settings: SecuritySettings | None = None,
) -> AccessTokenClaims:
    security = settings or get_settings().security
    payload = jwt.decode(
        token,
        security.jwt_secret_key.get_secret_value(),
        algorithms=[security.jwt_algorithm],
        options={"require": ["sub", "exp", "iat", "jti"]},
    )
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise jwt.InvalidTokenError("access token subject is missing")
    jti = payload.get("jti")
    if not isinstance(jti, str) or _ACCESS_JTI.fullmatch(jti) is None:
        raise jwt.InvalidTokenError("access token jti is invalid")
    role = payload.get("role")
    return AccessTokenClaims(
        username=username,
        role=role if isinstance(role, str) else "",
    )


def resolve_access_token_subject(token: str) -> str | None:
    """Resolve a stable authenticated principal without exposing JWT claims."""

    try:
        return decode_access_token(token).username
    except jwt.InvalidTokenError:
        return None


__all__ = [
    "AccessTokenClaims",
    "create_access_token",
    "decode_access_token",
    "resolve_access_token_subject",
]
