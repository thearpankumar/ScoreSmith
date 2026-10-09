"""First-admin provisioning.

Two deliberately different paths, chosen by `ENV`:

* NON-production (local dev, CI): `ensure_env_admin()` runs when the api / worker starts. If `ADMIN_EMAIL` and
  `ADMIN_PASSWORD` are set and no user has that email, it creates an email-verified admin. It never touches an
  existing user (no password reset, no role change), and the password is never logged. The env password is
  exempt from the strength policy (dev convenience: it is never read in production).
* Production: the env credentials are IGNORED (a warning is logged by `Settings.validate_production_settings`).
  The first admin is created through `POST /api/v1/auth/register-user` guarded by a one-time `BOOTSTRAP_TOKEN`
  (see app/api/v1/auth.py); that endpoint closes for good as soon as any user exists.

Everything that creates a user from nothing serialises on one Postgres advisory lock, so any number of api /
worker replicas starting (or racing HTTP requests) produce exactly one account.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.auth import security as sec
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models.enums import AuditAction
from app.models.user import ROLE_ADMIN, User

logger = logging.getLogger(__name__)

# Arbitrary constant: the key of the transaction-scoped advisory lock that serialises first-user creation.
_PROVISION_LOCK_KEY = 7_340_112_001


async def lock_user_provisioning(db: AsyncSession) -> None:
    """Takes the provisioning lock for the rest of the current transaction (released on commit / rollback)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PROVISION_LOCK_KEY})


async def user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def any_username(db: AsyncSession) -> bool:
    """True once at least one account has a username: the sign-in form then offers 'Email or username'."""
    return (await db.execute(select(User.id).where(User.username.is_not(None)).limit(1))).first() is not None


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time comparison (digests first, so the lengths do not leak either). An unset token never matches."""
    if not expected:
        return False
    a = hashlib.sha256(provided.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


async def ensure_env_admin() -> str:
    """Creates the ADMIN_EMAIL / ADMIN_PASSWORD admin outside production. Idempotent and replica-safe.

    Returns what happened: "created", "exists", "disabled" (production), "unconfigured" or "invalid"."""
    s = get_settings()
    email_raw = s.admin_email.strip()
    password = s.admin_password.get_secret_value()
    if s.is_production:
        return "disabled"  # warning is emitted by validate_production_settings()
    if not email_raw and not password:
        return "unconfigured"
    if not email_raw or not password:
        logger.warning("ADMIN_EMAIL and ADMIN_PASSWORD must both be set to create the first admin; skipping.")
        return "invalid"
    email = sec.normalize_email(email_raw)
    if not sec.looks_like_email(email):
        logger.warning("ADMIN_EMAIL is not a valid email address; skipping admin creation.")
        return "invalid"
    # Non-production only (production returned "disabled" above): the dev admin may use any non-empty password, so
    # a short one like a throwaway local login works. The strength policy still applies to every other path.

    password_hash = await sec.hash_password(password)  # slow: done before taking the lock
    async with AsyncSessionLocal() as db:
        await lock_user_provisioning(db)
        existing = (await db.execute(select(User.id).where(User.email == email))).first()
        if existing is not None:
            await db.rollback()
            logger.info("Admin %s already exists; leaving it untouched.", email)
            return "exists"
        user = User(
            email=email,
            name=email.split("@")[0],
            role=ROLE_ADMIN,
            password_hash=password_hash,
            email_verified_at=sec.utcnow(),
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:  # a concurrent signup took the address between the check and the insert
            await db.rollback()
            return "exists"
        audit(
            db,
            None,
            actor_id=None,
            entity_type="user",
            entity_id=user.id,
            action=AuditAction.CREATE,
            event="admin_provisioned_from_env",
        )
        await db.commit()
    logger.info("Created admin user %s from ADMIN_EMAIL / ADMIN_PASSWORD.", email)
    return "created"


async def ensure_env_admin_safe() -> None:
    """Startup hook: provisioning problems (e.g. DB not migrated yet) must never stop the process from booting."""
    try:
        await ensure_env_admin()
    except Exception:  # noqa: BLE001
        logger.warning("Could not provision the admin user at startup.", exc_info=True)
