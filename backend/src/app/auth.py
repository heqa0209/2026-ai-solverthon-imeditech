from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import LoginAttempt, Session, User
from app.schemas import AuthResponse, CsrfResponse, LoginInput, UserView

SESSION_COOKIE_NAME = "solverthon_session"
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,50}$")
LOGIN_WINDOW = timedelta(minutes=15)
SESSION_TTL = timedelta(hours=12)
PASSWORD_HASHER = PasswordHasher(type=Type.ID)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_username(value: str) -> str:
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "username must be 3-50 lowercase letters, numbers, dot, underscore or dash"
        )
    return normalized


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(value: str) -> str:
    if not value:
        raise ValueError("password must not be empty")
    return PASSWORD_HASHER.hash(value)


def verify_password(password_hash: str, value: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, value)
    except VerificationError, InvalidHashError:
        return False


def _is_expired(value: datetime, now: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= now


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: Session


async def current_auth(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "로그인이 필요합니다.")
    statement = (
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(Session.token_hash == hash_secret(token), Session.revoked_at.is_(None))
    )
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise ApiError(401, "SESSION_INVALID", "세션이 유효하지 않습니다.")
    session, user = row
    if _is_expired(session.expires_at, utc_now()) or not user.is_active:
        raise ApiError(401, "SESSION_EXPIRED", "세션이 만료되었습니다.")
    return AuthContext(user=user, session=session)


async def require_csrf(
    request: Request,
    auth: Annotated[AuthContext, Depends(current_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthContext:
    expected_origin = str(settings.app_origin).rstrip("/")
    origin = request.headers.get("Origin", "").rstrip("/")
    if not origin or origin != expected_origin:
        raise ApiError(403, "ORIGIN_MISMATCH", "요청 출처를 확인할 수 없습니다.")
    token = request.headers.get("X-CSRF-Token")
    if not token or not secrets.compare_digest(hash_secret(token), auth.session.csrf_hash):
        raise ApiError(403, "CSRF_INVALID", "보안 토큰이 유효하지 않습니다.")
    return auth


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginInput,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthResponse:
    try:
        username = normalize_username(body.username)
    except ValueError:
        username = body.username.strip().lower()[:50]
    ip_address = request.client.host if request.client else "unknown"
    window_start = utc_now() - LOGIN_WINDOW
    failed_count = await db.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.username == username,
            LoginAttempt.ip_address == ip_address,
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.attempted_at >= window_start,
        )
    )
    if (failed_count or 0) >= 5:
        raise ApiError(429, "LOGIN_RATE_LIMITED", "잠시 후 다시 시도해 주세요.")

    user = await db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
    if user is None or not verify_password(user.password_hash, body.password):
        db.add(
            LoginAttempt(
                username=username,
                ip_address=ip_address,
                succeeded=False,
                attempted_at=utc_now(),
            )
        )
        await db.commit()
        raise ApiError(401, "LOGIN_FAILED", "아이디 또는 비밀번호가 올바르지 않습니다.")

    token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(48)
    db.add(LoginAttempt(username=username, ip_address=ip_address, succeeded=True))
    db.add(
        Session(
            user_id=user.id,
            token_hash=hash_secret(token),
            csrf_hash=hash_secret(csrf_token),
            expires_at=utc_now() + SESSION_TTL,
        )
    )
    await db.commit()
    _set_session_cookie(response, token, settings)
    return AuthResponse(user=UserView(id=user.id, username=user.username))


@router.get("/me", response_model=AuthResponse)
async def me(auth: Annotated[AuthContext, Depends(current_auth)]) -> AuthResponse:
    return AuthResponse(user=UserView(id=auth.user.id, username=auth.user.username))


@router.get("/csrf", response_model=CsrfResponse)
async def csrf(
    auth: Annotated[AuthContext, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CsrfResponse:
    token = secrets.token_urlsafe(48)
    auth.session.csrf_hash = hash_secret(token)
    await db.commit()
    return CsrfResponse(csrfToken=token)


@router.post("/logout", status_code=204, response_class=Response)
async def logout(
    response: Response,
    auth: Annotated[AuthContext, Depends(require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    auth.session.revoked_at = utc_now()
    await db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.status_code = 204
    return response


async def create_user(db: AsyncSession, username: str, password: str) -> User:
    normalized = normalize_username(username)
    if await db.scalar(select(User.id).where(User.username == normalized)):
        raise ValueError(f"user already exists: {normalized}")
    user = User(username=normalized, password_hash=hash_password(password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def reset_password(db: AsyncSession, username: str, password: str) -> User:
    normalized = normalize_username(username)
    user = await db.scalar(select(User).where(User.username == normalized))
    if user is None:
        raise ValueError(f"user not found: {normalized}")
    user.password_hash = hash_password(password)
    await db.execute(
        update(Session)
        .where(Session.user_id == user.id, Session.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    await db.commit()
    return user
