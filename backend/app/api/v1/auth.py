"""Authentication endpoints: signup, login, refresh, logout, logout-all, me, forgot / reset password, verify e-mail.

Browser clients authenticate with httpOnly cookies (access JWT + rotating refresh token + a readable CSRF
cookie); scripts may use the `access_token` returned by login as `Authorization: Bearer`. See app/auth/."""

from __future__ import annotations

import hashlib
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import mailer
from app.audit import audit
from app.auth import bootstrap, service
from app.auth import security as sec
from app.auth.handles import find_user_by_handle
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models.enums import AuditAction
from app.models.user import ROLE_ADMIN, ROLE_USER, User
from app.ratelimit import rate_limit, within_limit
from app.schemas.user import (
    AuthConfigResponse,
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterUserRequest,
    ResetPasswordRequest,
    SignupRequest,
    UserMeUpdate,
    UserRead,
    VerifyEmailRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(prefix="/me", tags=["me"])

_BAD_LOGIN = "Invalid email or password."


def _email_key(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:32]


async def _find_user(db: AsyncSession, email: str) -> User | None:
    return (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()


async def _find_user_by_username(db: AsyncSession, username: str) -> User | None:
    return (
        await db.execute(select(User).where(func.lower(User.username) == username, User.deleted_at.is_(None)))
    ).scalar_one_or_none()


def _auth_response(user: User, access: str, expires_in: int) -> AuthResponse:
    return AuthResponse(user=UserRead.model_validate(user), access_token=access, expires_in=expires_in)


# --- signup / login -----------------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("signup", lambda s: s.rate_limit_signup))],
)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    sec.check_origin(request)
    if not get_settings().signup_allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Sign-up is disabled. Ask an administrator to create your account."
        )
    email = sec.normalize_email(payload.email)
    problem = sec.password_problem(payload.password, email)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)
    if await _find_user(db, email) is not None:
        # Accounts that were created before login existed have no password: they are claimed through
        # "Forgot password", which proves ownership of the address.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Sign in, or use 'Forgot password' to set a password.",
        )
    user = User(
        email=email,
        name=payload.name.strip(),
        role=ROLE_USER,
        password_hash=await sec.hash_password(payload.password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    raw = await service.create_email_token(db, user, "verify")
    access, expires_in = await service.start_session(db, user, request, response, remember=False)
    audit(
        db, request, actor_id=user.id, entity_type="auth", entity_id=user.id, action=AuditAction.CREATE, event="signup"
    )
    await db.commit()
    background.add_task(_verification_mail_task, user.email, user.name, raw)
    return _auth_response(user, access, expires_in)


async def _verification_mail_task(email: str, name: str, raw: str) -> None:
    link = f"{get_settings().frontend_url.rstrip('/')}/verify-email?token={raw}"
    await mailer.send_email(
        email,
        "Verify your email address",
        f"Hi {name},\n\nConfirm your email address to finish setting up your Score Smith account:\n\n{link}\n\n"
        f"This link works once and expires in {get_settings().email_verify_ttl_hours} hours. If you did not create "
        "an account you can ignore this message.\n",
    )


@router.post(
    "/login",
    response_model=AuthResponse,
    dependencies=[Depends(rate_limit("login", lambda s: s.rate_limit_login))],
)
async def login(
    payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> AuthResponse:
    sec.check_origin(request)
    s = get_settings()
    identifier = payload.email.strip()
    if s.dev_username_login and "@" not in identifier and identifier.lower() == s.admin_username.strip().lower():
        identifier = s.admin_email  # dev-only shortcut: `admin` -> the ADMIN_EMAIL account (never in production)
    email = sec.normalize_email(identifier)
    # Per-address throttle (also covers addresses that do not exist, so it reveals nothing).
    if not await within_limit("login-email", s.rate_limit_login, _email_key(email)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Try again in a few minutes.",
            headers={"Retry-After": "60"},
        )
    # Sign-in accepts the email or (since migration 0013) the account's username.
    user = await find_user_by_handle(db, email)
    if user is not None:
        remaining = service.lockout_remaining_seconds(user)
        if remaining > 0:
            audit(
                db,
                request,
                actor_id=user.id,
                entity_type="auth",
                entity_id=user.id,
                action=AuditAction.UPDATE,
                event="login_blocked_locked",
            )
            await db.commit()
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts. This account is temporarily locked; try again later.",
                headers={"Retry-After": str(remaining)},
            )
    valid = await sec.verify_password(payload.password, user.password_hash if user else None)
    if user is None or not valid or not user.is_active:
        if user is not None:
            await service.register_failed_login(db, user)
            audit(
                db,
                request,
                actor_id=user.id,
                entity_type="auth",
                entity_id=user.id,
                action=AuditAction.UPDATE,
                event="login_failed",
            )
            await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_BAD_LOGIN)
    await service.clear_failed_logins(db, user)
    if user.password_hash and sec.needs_rehash(user.password_hash):
        user.password_hash = await sec.hash_password(payload.password)
    access, expires_in = await service.start_session(db, user, request, response, remember=payload.remember_me)
    audit(
        db, request, actor_id=user.id, entity_type="auth", entity_id=user.id, action=AuditAction.CREATE, event="login"
    )
    await db.commit()
    return _auth_response(user, access, expires_in)


@router.post(
    "/refresh",
    response_model=AuthResponse,
    responses={401: {"model": MessageResponse}},
    dependencies=[Depends(rate_limit("refresh", lambda s: s.rate_limit_refresh))],
)
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    sec.verify_csrf(request)
    raw = request.cookies.get(sec.REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    try:
        user, access, expires_in = await service.rotate_refresh(db, request, response, raw)
    except HTTPException as exc:
        # The cookies are dead: clear them so the browser stops presenting them. (A raised HTTPException would
        # drop headers set on `response`, so answer with an explicit JSONResponse.)
        failed = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        sec.clear_auth_cookies(failed)
        return failed
    await db.commit()
    return _auth_response(user, access, expires_in)


@router.post("/logout", response_model=MessageResponse)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    """Ends this browser's session. Works with an expired access token (the refresh cookie identifies the
    session); a request that carries a CSRF cookie must also carry the matching header."""
    if request.cookies.get(sec.CSRF_COOKIE) or request.cookies.get(sec.ACCESS_COOKIE):
        sec.verify_csrf(request)
    else:
        sec.check_origin(request)
    user_id = await service.revoke_presented_refresh(db, request.cookies.get(sec.REFRESH_COOKIE))
    if user_id is not None:
        audit(
            db,
            request,
            actor_id=user_id,
            entity_type="auth",
            entity_id=user_id,
            action=AuditAction.DELETE,
            event="logout",
        )
    await db.commit()
    sec.clear_auth_cookies(response)
    return MessageResponse(detail="Signed out.")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(
    request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> MessageResponse:
    await service.revoke_all_sessions(db, user)
    audit(
        db,
        request,
        actor_id=user.id,
        entity_type="auth",
        entity_id=user.id,
        action=AuditAction.DELETE,
        event="logout_all",
    )
    await db.commit()
    sec.clear_auth_cookies(response)
    return MessageResponse(detail="Signed out everywhere.")


@router.get("/me", response_model=UserRead)
async def auth_me(user: User = Depends(get_current_user)) -> User:
    return user


# --- first-run config + register-user (bootstrap / admin-created accounts) ------------------------------


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config(db: AsyncSession = Depends(get_db)) -> AuthConfigResponse:
    """What the sign-in pages need: whether to offer 'Sign up', and whether the first-run setup page applies."""
    s = get_settings()
    setup = bool(s.bootstrap_token.get_secret_value()) and await bootstrap.user_count(db) == 0
    return AuthConfigResponse(
        signup_enabled=s.signup_allowed,
        setup_required=setup,
        username_login=True,  # handles (username, or the part of the email before the @) always sign in
    )


@router.post(
    "/register-user",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register-user", lambda s: s.rate_limit_register_user))],
)
async def register_user(payload: RegisterUserRequest, request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Creates an account without self-service signup.

    * Signed in as an ADMIN: creates a user (role `member` by default, or `admin`).
    * Anonymous, while ZERO users exist and BOOTSTRAP_TOKEN is configured: the token in `X-Bootstrap-Token`
      (compared in constant time) creates the first admin. This path closes permanently once any user exists.
    * Everything else answers 404 (closed / disabled), or 403 for a non-admin / a wrong bootstrap token.
    """
    sec.check_origin(request)
    s = get_settings()
    email = sec.normalize_email(payload.email)

    actor: User | None = None
    stale_auth: HTTPException | None = None
    token, _via_cookie = sec.extract_access_token(request)
    if token:
        try:
            actor = await get_current_user(request, db)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_401_UNAUTHORIZED:
                raise
            stale_auth = exc  # a leftover cookie must not block first-run setup; re-raised below if not first run

    first_run = False
    if actor is not None:
        if actor.role != ROLE_ADMIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Administrator access required.")
        role = payload.role or ROLE_USER
    else:
        expected = s.bootstrap_token.get_secret_value()
        if not expected or await bootstrap.user_count(db) > 0:
            if stale_auth is not None:
                raise stale_auth
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found.")
        if not bootstrap.token_matches(request.headers.get("x-bootstrap-token", ""), expected):
            audit(
                db,
                request,
                actor_id=None,
                entity_type="auth",
                entity_id=uuid.uuid4(),
                action=AuditAction.UPDATE,
                event="bootstrap_token_rejected",
            )
            await db.commit()
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid bootstrap token.")
        first_run = True
        role = ROLE_ADMIN

    problem = (
        sec.admin_password_problem(payload.password, email)
        if role == ROLE_ADMIN
        else sec.password_problem(payload.password, email)
    )
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)
    password_hash = await sec.hash_password(payload.password)

    await bootstrap.lock_user_provisioning(db)
    if first_run and await bootstrap.user_count(db) > 0:  # lost the race: someone else became the first user
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found.")
    if await _find_user(db, email) is not None:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
    user = User(
        email=email,
        name=payload.name.strip(),
        role=role,
        password_hash=password_hash,
        email_verified_at=sec.utcnow(),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    audit(
        db,
        request,
        actor_id=actor.id if actor else user.id,
        entity_type="user",
        entity_id=user.id,
        action=AuditAction.CREATE,
        event="bootstrap_admin_created" if first_run else "user_registered_by_admin",
        diff={"role": role},
    )
    await db.commit()
    if first_run:
        logger.info("First admin %s created through the bootstrap endpoint; the endpoint is now closed.", email)
    return user


# --- forgot / reset / verify --------------------------------------------------------------------------

_FORGOT_REPLY = "If an account exists for that email, a reset link is on its way."


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("forgot", lambda s: s.rate_limit_forgot))],
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Same answer whether or not the address has an account (no enumeration)."""
    sec.check_origin(request)
    email = sec.normalize_email(payload.email)
    user = await _find_user(db, email)
    if user is not None and user.is_active and await within_limit("forgot-email", "3/hour", _email_key(email)):
        raw = await service.create_email_token(db, user, "reset")
        audit(
            db,
            request,
            actor_id=user.id,
            entity_type="auth",
            entity_id=user.id,
            action=AuditAction.CREATE,
            event="password_reset_requested",
        )
        await db.commit()
        link = f"{get_settings().frontend_url.rstrip('/')}/reset-password?token={raw}"
        minutes = get_settings().password_reset_ttl_minutes
        background.add_task(
            mailer.send_email,
            user.email,
            "Reset your password",
            f"Hi {user.name},\n\nUse this link to choose a new password:\n\n{link}\n\nIt works once and expires in "
            f"{minutes} minutes. If you did not ask for this you can ignore the message; "
            "your password has not changed.\n",
        )
    return MessageResponse(detail=_FORGOT_REPLY)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("reset", lambda s: s.rate_limit_reset))],
)
async def reset_password(
    payload: ResetPasswordRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    sec.check_origin(request)
    # Validate the new password BEFORE spending the single-use token.
    peek = (
        await db.execute(
            select(User)
            .join(service.PasswordResetToken, service.PasswordResetToken.user_id == User.id)
            .where(service.PasswordResetToken.token_hash == sec.hash_token(payload.token))
        )
    ).scalar_one_or_none()
    problem = sec.password_problem(payload.password, peek.email if peek else None)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)
    user = await service.consume_email_token(db, payload.token, "reset")
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This reset link is invalid or has expired.")
    user.password_hash = await sec.hash_password(payload.password)
    if user.email_verified_at is None:  # the link reached their inbox
        user.email_verified_at = sec.utcnow()
    user.failed_logins = 0
    user.locked_until = None
    await service.revoke_all_sessions(db, user)
    audit(
        db,
        request,
        actor_id=user.id,
        entity_type="auth",
        entity_id=user.id,
        action=AuditAction.UPDATE,
        event="password_reset",
    )
    await db.commit()
    sec.clear_auth_cookies(response)
    return MessageResponse(detail="Your password has been updated. Sign in with the new password.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    payload: VerifyEmailRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    sec.check_origin(request)
    user = await service.consume_email_token(db, payload.token, "verify")
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This verification link is invalid or has expired.")
    if user.email_verified_at is None:
        user.email_verified_at = sec.utcnow()
    audit(
        db,
        request,
        actor_id=user.id,
        entity_type="auth",
        entity_id=user.id,
        action=AuditAction.UPDATE,
        event="email_verified",
    )
    await db.commit()
    return MessageResponse(detail="Email verified.")


@router.post("/resend-verification", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageResponse:
    if user.email_verified_at is None and await service.recent_token_count(db, user, "verify", 60) < 3:
        raw = await service.create_email_token(db, user, "verify")
        await db.commit()
        background.add_task(_verification_mail_task, user.email, user.name, raw)
    return MessageResponse(detail="If your email is not verified yet, a new link is on its way.")


# --- /me ----------------------------------------------------------------------------------------------


@me_router.get("", response_model=UserRead)
async def get_me(user: User = Depends(get_current_user)) -> User:
    return user


@me_router.patch("", response_model=UserRead)
async def update_me(
    payload: UserMeUpdate, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> User:
    user.name = payload.name.strip()
    audit(
        db,
        request,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        action=AuditAction.UPDATE,
        event="profile_updated",
    )
    await db.commit()
    await db.refresh(user)
    return user


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


@me_router.post(
    "/password",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("change-password", lambda s: "10/hour", per_user=True))],
)
async def change_my_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageResponse:
    """Own password change (the Account dialog in the user menu - normal users have no Settings page). Needs the
    current password; every session, including this one, is signed out afterwards."""
    if not await sec.verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="The current password is not correct.")
    policy = sec.admin_password_problem if user.role == ROLE_ADMIN else sec.password_problem
    problem = policy(payload.new_password, user.email)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)
    user.password_hash = await sec.hash_password(payload.new_password)
    await service.revoke_all_sessions(db, user)
    audit(db, request, actor_id=user.id, entity_type="auth", entity_id=user.id, action=AuditAction.UPDATE,
          event="password_changed")
    await db.commit()
    sec.clear_auth_cookies(response)
    return MessageResponse(detail="Password changed. Please sign in again.")
