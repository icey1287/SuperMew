import base64
import hashlib
import hmac
import os
import re
from collections.abc import Generator

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from backend.auth.access import decode_access_token
from backend.core.settings import get_settings
from backend.db.models import User
from backend.infra.database import SessionLocal

_settings = get_settings().security
ADMIN_INVITE_CODE = _settings.admin_invite_code.get_secret_value()
PBKDF2_ROUNDS = _settings.password_pbkdf2_rounds

_BCRYPT_SHA256_V1_RE = re.compile(
    r"^\$bcrypt-sha256\$(?P<type>2[ab]),(?P<rounds>\d{1,2})"
    r"\$(?P<salt>[^$]{22})\$(?P<digest>[^$]{31})$"
)
_BCRYPT_SHA256_V2_RE = re.compile(
    r"^\$bcrypt-sha256\$v=2,t=(?P<type>2b),r=(?P<rounds>\d{1,2})"
    r"\$(?P<salt>[^$]{22})\$(?P<digest>[^$]{31})$"
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_password(plain_password: str, password_hash: str) -> bool:
    if not plain_password or not password_hash:
        return False

    # New format: pbkdf2_sha256$<rounds>$<salt_b64>$<digest_b64>
    if password_hash.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt_b64, digest_b64 = password_hash.split("$", 3)
            salt = base64.b64decode(salt_b64.encode("ascii"))
            expected = base64.b64decode(digest_b64.encode("ascii"))
            calculated = hashlib.pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                salt,
                int(rounds),
            )
            return hmac.compare_digest(calculated, expected)
        except Exception:
            return False

    # Backward compatibility for legacy bcrypt and Passlib bcrypt_sha256 hashes.
    if password_hash.startswith("$2"):
        try:
            # Historical bcrypt implementations truncate after 72 bytes. bcrypt 5
            # rejects longer inputs, so reproduce the old verification semantics.
            return bcrypt.checkpw(
                plain_password.encode("utf-8")[:72],
                password_hash.encode("ascii"),
            )
        except (TypeError, ValueError, UnicodeError):
            return False
    if password_hash.startswith("$bcrypt-sha256$"):
        return _verify_legacy_bcrypt_sha256(plain_password, password_hash)

    return False


def get_password_hash(password: str) -> str:
    if not password:
        raise ValueError("password is required")

    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ROUNDS,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt_b64}${digest_b64}"


def _verify_legacy_bcrypt_sha256(plain_password: str, password_hash: str) -> bool:
    match = _BCRYPT_SHA256_V2_RE.fullmatch(password_hash)
    version = 2
    if match is None:
        match = _BCRYPT_SHA256_V1_RE.fullmatch(password_hash)
        version = 1
    if match is None:
        return False

    try:
        rounds = int(match.group("rounds"))
        if not 4 <= rounds <= 31:
            return False
        salt = match.group("salt")
        password_bytes = plain_password.encode("utf-8")
        if version == 1:
            digest = hashlib.sha256(password_bytes).digest()
        else:
            digest = hmac.new(
                salt.encode("ascii"),
                password_bytes,
                hashlib.sha256,
            ).digest()
        prehash = base64.b64encode(digest)
        bcrypt_hash = (
            f"${match.group('type')}${rounds:02d}${salt}{match.group('digest')}"
        ).encode("ascii")
        return bcrypt.checkpw(prehash, bcrypt_hash)
    except (TypeError, ValueError, UnicodeError):
        return False


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.password_hash.startswith("pbkdf2_sha256$"):
        user.password_hash = get_password_hash(password)
    return user


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或过期的认证令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        username = decode_access_token(token).username
    except InvalidTokenError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise credentials_exception
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="管理员权限不足"
        )
    return current_user


def resolve_role(requested_role: str | None, admin_code: str | None) -> str:
    role = (requested_role or "user").strip().lower()
    if role != "admin":
        return "user"
    if ADMIN_INVITE_CODE and hmac.compare_digest(
        admin_code or "",
        ADMIN_INVITE_CODE,
    ):
        return "admin"
    raise HTTPException(status_code=403, detail="管理员邀请码错误")
