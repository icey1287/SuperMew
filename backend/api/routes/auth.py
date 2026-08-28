from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from backend.auth.identity import normalize_username
from backend.auth.origin import enforce_trusted_auth_origin
from backend.auth.service import RefreshTokenRejected, TokenGrant, token_service
from backend.db.models import User
from backend.infra.auth import (
    authenticate_user,
    get_current_user,
    get_db,
    get_password_hash,
    resolve_role,
)
from backend.rate_limits.auth import enforce_auth_username_rate_limit
from backend.schemas import (
    AuthResponse,
    CurrentUserResponse,
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
)


router = APIRouter(tags=["auth"])


def _auth_response(grant: TokenGrant) -> AuthResponse:
    return AuthResponse(
        access_token=grant.access_token,
        username=grant.username,
        role=grant.role,
    )


def _refresh_cookie(request: Request) -> str | None:
    return request.cookies.get(token_service.cookie_name)


def _trusted_auth_origin(request: Request) -> None:
    enforce_trusted_auth_origin(request, settings=token_service.settings)


def _normalized_username(value: str) -> str:
    username = normalize_username(value)
    if not username or len(username) > 100:
        raise HTTPException(status_code=400, detail="用户名长度无效")
    return username


def _reject_refresh(response: Response, exc: RefreshTokenRejected) -> NoReturn:
    token_service.clear_refresh_cookie(response)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或过期的刷新令牌",
        headers={
            "WWW-Authenticate": "Bearer",
            "Set-Cookie": response.headers["set-cookie"],
        },
    ) from exc


@router.post("/auth/register", response_model=AuthResponse)
def register(
    request: RegisterRequest,
    response: Response,
    _origin_guard: None = Depends(_trusted_auth_origin),
    _rate_limit: None = Depends(enforce_auth_username_rate_limit),
    db: Session = Depends(get_db),
) -> AuthResponse:
    username = _normalized_username(request.username)
    password = request.password or ""
    if not password.strip():
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    exists = db.query(User).filter(User.username == username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")

    role = resolve_role(request.role, request.admin_code)
    user = User(username=username, password_hash=get_password_hash(password), role=role)
    db.add(user)
    grant = token_service.issue(db, user)
    token_service.set_refresh_cookie(response, grant.refresh_token)
    return _auth_response(grant)


@router.post("/auth/login", response_model=AuthResponse)
def login(
    request: LoginRequest,
    response: Response,
    _origin_guard: None = Depends(_trusted_auth_origin),
    _rate_limit: None = Depends(enforce_auth_username_rate_limit),
    db: Session = Depends(get_db),
) -> AuthResponse:
    username = _normalized_username(request.username)
    user = authenticate_user(db, username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    grant = token_service.issue(db, user)
    token_service.set_refresh_cookie(response, grant.refresh_token)
    return _auth_response(grant)


@router.post("/auth/refresh", response_model=AuthResponse)
def refresh(
    request: Request,
    response: Response,
    _origin_guard: None = Depends(_trusted_auth_origin),
    db: Session = Depends(get_db),
) -> AuthResponse:
    try:
        grant = token_service.rotate(db, _refresh_cookie(request))
    except RefreshTokenRejected as exc:
        _reject_refresh(response, exc)
    token_service.set_refresh_cookie(response, grant.refresh_token)
    return _auth_response(grant)


@router.post("/auth/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    response: Response,
    _origin_guard: None = Depends(_trusted_auth_origin),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    revoked = token_service.logout(db, _refresh_cookie(request))
    token_service.clear_refresh_cookie(response)
    return LogoutResponse(message="已退出登录", revoked_count=revoked)


@router.post("/auth/logout-all", response_model=LogoutResponse)
def logout_all(
    response: Response,
    _origin_guard: None = Depends(_trusted_auth_origin),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LogoutResponse:
    revoked = token_service.logout_all(db, user_id=current_user.id)
    token_service.clear_refresh_cookie(response)
    return LogoutResponse(message="已退出所有设备", revoked_count=revoked)


@router.get("/auth/me", response_model=CurrentUserResponse)
def me(current_user: User = Depends(get_current_user)) -> CurrentUserResponse:
    return CurrentUserResponse(username=current_user.username, role=current_user.role)
