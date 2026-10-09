"""Auth business logic over the tables: sessions (access JWT + rotating refresh token), lockout, one-time e-mail
tokens. The HTTP layer is app/api/v1/auth.py."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit, request_context
from app.auth import security as sec
from app.config import get_settings
from app.models.auth import PasswordResetToken, RefreshToken
from app.models.enums import AuditAction
from app.models.user import User

logger = logging.getLogger(__name__)


# --- sessions -----------------------------------------------------------------------------------------


async def start_session(
    db: AsyncSession,
    user: User,
    request: Request,
    response: Response,
    *,
    remember: bool,
    family_id: uuid.UUID | None = None,
) -> tuple[str, int]:
    """Creates a refresh token (new family unless rotating) and sets the access / refresh / CSRF cookies.
    Returns (access_token, expires_in_seconds). The caller commits."""
    s = get_settings()
    now = sec.utcnow()
    raw = sec.new_opaque_token()
    lifetime = timedelta(days=s.refresh_days_remember) if remember else timedelta(hours=s.refresh_hours_session)
    db.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=sec.hash_token(raw),
            remember=remember,
            expires_at=now + lifetime,
            **{k: v for k, v in request_context(request).items() if k in ("ip", "user_agent")},
        )
    )
    access, expires_in = sec.create_access_token(user.id, user.role)
    sec.set_access_cookie(response, access, expires_in)
    sec.set_refresh_cookie(response, raw, remember)
    csrf = request.cookies.get(sec.CSRF_COOKIE) if family_id else None
    sec.set_csrf_cookie(
        response, csrf or secrets.token_urlsafe(32), s.refresh_days_remember * 86400 if remember else None
    )
    return access, expires_in


async def revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=sec.utcnow())
    )


async def revoke_all_sessions(db: AsyncSession, user: User) -> None:
    """Logout everywhere: revoke every refresh token and invalidate access tokens issued so far."""
    now = sec.utcnow()
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    user.sessions_valid_after = now


def _unauthorized(detail: str = "Session expired. Please sign in again.") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail)


async def rotate_refresh(db: AsyncSession, request: Request, response: Response, raw: str) -> tuple[User, str, int]:
    """Exchanges a refresh token for a new access + refresh pair. Presenting an already-rotated token (outside a
    short grace window for two tabs refreshing together) is treated as theft and revokes the whole family."""
    s = get_settings()
    now = sec.utcnow()
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sec.hash_token(raw)).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise _unauthorized()
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    if row.revoked_at is not None or row.expires_at <= now:
        raise _unauthorized()
    if row.used_at is not None:
        if (now - row.used_at).total_seconds() <= s.refresh_reuse_grace_seconds:
            # Concurrent refresh (another tab already rotated this token): hand out an access token only; the
            # browser's cookie jar already holds the newer refresh token from the winning response.
            access, expires_in = sec.create_access_token(user.id, user.role)
            sec.set_access_cookie(response, access, expires_in)
            return user, access, expires_in
        await revoke_family(db, row.family_id)
        audit(
            db,
            request,
            actor_id=user.id,
            entity_type="auth",
            entity_id=user.id,
            action=AuditAction.UPDATE,
            event="refresh_reuse_detected",
            diff={"family_id": str(row.family_id)},
        )
        await db.commit()
        logger.warning("Refresh token reuse detected: user=%s family=%s (family revoked)", user.id, row.family_id)
        raise _unauthorized()
    row.used_at = now
    access, expires_in = await start_session(
        db, user, request, response, remember=row.remember, family_id=row.family_id
    )
    return user, access, expires_in


async def revoke_presented_refresh(db: AsyncSession, raw: str | None) -> uuid.UUID | None:
    """Revokes the family of the refresh token in the logout request; returns its user id."""
    if not raw:
        return None
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sec.hash_token(raw)))
    ).scalar_one_or_none()
    if row is None:
        return None
    await revoke_family(db, row.family_id)
    return row.user_id


# --- lockout ------------------------------------------------------------------------------------------


def lockout_remaining_seconds(user: User) -> int:
    if user.locked_until is None:
        return 0
    return max(0, int((user.locked_until - sec.utcnow()).total_seconds()))


async def register_failed_login(db: AsyncSession, user: User) -> None:
    """Atomically counts a bad password; from `lockout_threshold` failures on, locks the account with doubling
    backoff (`lockout_base_minutes`, capped at `lockout_max_minutes`)."""
    s = get_settings()
    failures = (
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(failed_logins=User.failed_logins + 1)
            .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
            .returning(User.failed_logins)
        )
    ).scalar_one()
    if failures >= s.lockout_threshold:
        minutes = min(s.lockout_base_minutes * 2 ** (failures - s.lockout_threshold), s.lockout_max_minutes)
        await db.execute(
            update(User).where(User.id == user.id).values(locked_until=sec.utcnow() + timedelta(minutes=minutes))
        )


async def clear_failed_logins(db: AsyncSession, user: User) -> None:
    if user.failed_logins or user.locked_until:
        user.failed_logins = 0
        user.locked_until = None


# --- one-time e-mail tokens ---------------------------------------------------------------------------


async def create_email_token(db: AsyncSession, user: User, purpose: str) -> str:
    s = get_settings()
    ttl = (
        timedelta(minutes=s.password_reset_ttl_minutes)
        if purpose == "reset"
        else timedelta(hours=s.email_verify_ttl_hours)
    )
    raw = sec.new_opaque_token()
    db.add(
        PasswordResetToken(
            user_id=user.id, purpose=purpose, token_hash=sec.hash_token(raw), expires_at=sec.utcnow() + ttl
        )
    )
    return raw


async def consume_email_token(db: AsyncSession, raw: str, purpose: str) -> User | None:
    """Marks the token used and returns its user, atomically: a token works exactly once."""
    now = sec.utcnow()
    user_id = (
        await db.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == sec.hash_token(raw),
                PasswordResetToken.purpose == purpose,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
            .returning(PasswordResetToken.user_id)
        )
    ).scalar_one_or_none()
    if user_id is None:
        return None
    return await db.get(User, user_id)


async def recent_token_count(db: AsyncSession, user: User, purpose: str, minutes: int = 60) -> int:
    from sqlalchemy import func

    return (
        await db.execute(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.purpose == purpose,
                PasswordResetToken.created_at > sec.utcnow() - timedelta(minutes=minutes),
            )
        )
    ).scalar_one()
