"""Admin user management (`/api/v1/admin/users`). Every route requires the admin role (`require_admin` -> 403),
is rate limited per admin and audit-logged. Admins manage ACCOUNTS only: they get no access to other users' charts or
chats (see app/authz.py)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import user_admin as ua
from app.audit import audit
from app.auth import security as sec
from app.auth import service as auth_service
from app.auth.bootstrap import lock_user_provisioning
from app.db import get_db
from app.deps import require_admin
from app.models.enums import AuditAction
from app.models.user import ROLE_ADMIN, ROLE_SYSTEM, User
from app.ratelimit import rate_limit
from app.schemas.user import DisplayName

router = APIRouter(
    prefix="/admin/users",
    tags=["admin"],
    dependencies=[Depends(rate_limit("admin", lambda s: s.rate_limit_admin, per_user=True))],
)

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


class AdminUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str | None
    email: str
    name: str
    role: str
    is_active: bool
    email_verified: bool
    created_at: datetime


class AdminUserList(BaseModel):
    items: list[AdminUserRead]
    total: int


class AdminUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: DisplayName
    username: str | None = Field(default=None, max_length=64)
    role: Literal["admin", "user"] = "user"
    password: str = Field(min_length=1, max_length=1024)


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None
    name: DisplayName | None = None
    username: str | None = Field(default=None, max_length=64)
    role: Literal["admin", "user"] | None = None
    clear_username: bool = False


class PasswordSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": error, "message": message})


def _read(user: User) -> AdminUserRead:
    return AdminUserRead(
        id=user.id, username=user.username, email=user.email, name=user.name, role=user.role,
        is_active=user.is_active, email_verified=user.email_verified, created_at=user.created_at,
    )


def _clean_username(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if not _USERNAME_RE.match(value):
        raise _err(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_username",
            "Usernames are 2-63 characters: letters, digits, dot, dash or underscore, starting with a letter or digit.",
        )
    return value


async def _target(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None), User.role != ROLE_SYSTEM))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


async def _unique(db: AsyncSession, *, email: str | None, username: str | None, exclude: uuid.UUID | None) -> None:
    if email is not None:
        stmt = select(User.id).where(func.lower(User.email) == email)
        if exclude:
            stmt = stmt.where(User.id != exclude)
        if (await db.execute(stmt)).first():
            raise _err(status.HTTP_409_CONFLICT, "email_taken", "An account with that email already exists.")
    if username is not None:
        stmt = select(User.id).where(func.lower(User.username) == username)
        if exclude:
            stmt = stmt.where(User.id != exclude)
        if (await db.execute(stmt)).first():
            raise _err(status.HTTP_409_CONFLICT, "username_taken", "That username is already taken.")


def _rule(exc: ua.AdminRuleError) -> HTTPException:
    return _err(status.HTTP_409_CONFLICT, exc.code, exc.message)


@router.get("", response_model=AdminUserList)
async def list_users(
    q: str | None = Query(default=None, max_length=200),
    role: Literal["admin", "user"] | None = None,
    active: bool | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> AdminUserList:
    stmt = select(User).where(User.deleted_at.is_(None), User.role != ROLE_SYSTEM)
    if q and q.strip():
        pat = "%" + q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        stmt = stmt.where(or_(User.email.ilike(pat, escape="\\"), User.name.ilike(pat, escape="\\"),
                              User.username.ilike(pat, escape="\\")))
    if role:
        stmt = stmt.where(User.role == role)
    if active is not None:
        stmt = stmt.where(User.is_active.is_(active))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(func.lower(User.name), User.id).offset(skip).limit(limit))).scalars().all()
    return AdminUserList(items=[_read(u) for u in rows], total=int(total))


@router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
) -> AdminUserRead:
    email = sec.normalize_email(payload.email)
    username = _clean_username(payload.username)
    problem = (
        sec.admin_password_problem(payload.password, email)
        if payload.role == ROLE_ADMIN
        else sec.password_problem(payload.password, email)
    )
    if problem:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "weak_password", problem)
    password_hash = await sec.hash_password(payload.password)
    await lock_user_provisioning(db)
    await _unique(db, email=email, username=username, exclude=None)
    user = User(
        email=email, name=payload.name.strip(), username=username, role=payload.role, password_hash=password_hash,
        email_verified_at=sec.utcnow(),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _err(status.HTTP_409_CONFLICT, "duplicate", "That email or username is already taken.") from exc
    audit(db, request, actor_id=admin.id, entity_type="user", entity_id=user.id, action=AuditAction.CREATE,
          event="admin_user_created", diff={"role": payload.role})
    await db.commit()
    return _read(user)


@router.patch("/{user_id}", response_model=AdminUserRead)
async def update_user(
    user_id: uuid.UUID, payload: AdminUserUpdate, request: Request, db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminUserRead:
    user = await _target(db, user_id)
    changes: dict = {}
    email = sec.normalize_email(payload.email) if payload.email else None
    username = _clean_username(payload.username) if payload.username is not None else None
    await lock_user_provisioning(db)
    await _unique(db, email=email, username=username, exclude=user.id)
    role_changed = payload.role is not None and payload.role != user.role
    if role_changed:
        try:
            await ua.guard_admin_change(
                db, admin, user, losing_admin=payload.role != ROLE_ADMIN, what="change the role of"
            )
        except ua.AdminRuleError as exc:
            raise _rule(exc) from exc
        user.role = payload.role  # type: ignore[assignment]
        changes["role"] = payload.role
        await auth_service.revoke_all_sessions(db, user)  # a role change takes effect on the next sign-in
    if email is not None and email != user.email:
        user.email, changes["email"] = email, True
        user.email_verified_at = sec.utcnow()
    if payload.name is not None and payload.name.strip() != user.name:
        user.name, changes["name"] = payload.name.strip(), True
    if payload.clear_username:
        user.username, changes["username"] = None, True
    elif username is not None and username != user.username:
        user.username, changes["username"] = username, True
    audit(db, request, actor_id=admin.id, entity_type="user", entity_id=user.id, action=AuditAction.UPDATE,
          event="admin_user_updated", diff={"fields": sorted(changes)})
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _err(status.HTTP_409_CONFLICT, "duplicate", "That email or username is already taken.") from exc
    return _read(user)


@router.post("/{user_id}/password", response_model=AdminUserRead)
async def set_password(
    user_id: uuid.UUID, payload: PasswordSet, request: Request, db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminUserRead:
    user = await _target(db, user_id)
    problem = (
        sec.admin_password_problem(payload.password, user.email)
        if user.role == ROLE_ADMIN
        else sec.password_problem(payload.password, user.email)
    )
    if problem:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "weak_password", problem)
    user.password_hash = await sec.hash_password(payload.password)
    user.failed_logins, user.locked_until = 0, None
    await auth_service.revoke_all_sessions(db, user)
    audit(db, request, actor_id=admin.id, entity_type="user", entity_id=user.id, action=AuditAction.UPDATE,
          event="admin_password_set")
    await db.commit()
    return _read(user)


@router.post("/{user_id}/deactivate", response_model=AdminUserRead)
async def deactivate_user(
    user_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
) -> AdminUserRead:
    user = await _target(db, user_id)
    try:
        await ua.guard_admin_change(db, admin, user, losing_admin=True, what="deactivate")
    except ua.AdminRuleError as exc:
        raise _rule(exc) from exc
    await ua.deactivate(db, user)
    audit(db, request, actor_id=admin.id, entity_type="user", entity_id=user.id, action=AuditAction.UPDATE,
          event="admin_user_deactivated")
    await db.commit()
    await ua.stop_user_work(user.id)  # free their evaluation / chat slots
    return _read(user)


@router.post("/{user_id}/reactivate", response_model=AdminUserRead)
async def reactivate_user(
    user_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
) -> AdminUserRead:
    user = await _target(db, user_id)
    user.is_active = True
    user.failed_logins, user.locked_until = 0, None
    audit(db, request, actor_id=admin.id, entity_type="user", entity_id=user.id, action=AuditAction.UPDATE,
          event="admin_user_reactivated")
    await db.commit()
    return _read(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_user(
    user_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
) -> None:
    user = await _target(db, user_id)
    try:
        await ua.guard_admin_change(db, admin, user, losing_admin=True, what="delete")
    except ua.AdminRuleError as exc:
        raise _rule(exc) from exc
    await ua.stop_user_work(user.id)  # free their slots first; the rows below are then terminal
    await db.refresh(user)
    summary = await ua.anonymise_and_delete(db, user)
    chat_ids = summary.pop("chat_ids", [])
    audit(db, request, actor_id=admin.id, entity_type="user", entity_id=user_id, action=AuditAction.DELETE,
          event="admin_user_deleted", diff=summary)
    await db.commit()
    await ua.drop_checkpoints(chat_ids)

