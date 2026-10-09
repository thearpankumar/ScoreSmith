"""Resolving what a person types into a sign-in / invite box to ONE account.

An identifier is an email address (contains "@") or a handle. A handle matches a username (case-insensitive); when
nobody has that username it falls back to the part of an email address before the "@" - but only if exactly one
account has it (an ambiguous handle never resolves, so nobody can be reached by accident). This is what makes
"sign in as arpankumar1119" work for accounts created before usernames existed."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import ROLE_SYSTEM, User


async def find_user_by_handle(db: AsyncSession, identifier: str, *, active_only: bool = False) -> User | None:
    ident = identifier.strip().lower()
    if not ident:
        return None
    base = select(User).where(User.deleted_at.is_(None), User.role != ROLE_SYSTEM)
    if active_only:
        base = base.where(User.is_active.is_(True))
    if "@" in ident:
        return (await db.execute(base.where(func.lower(User.email) == ident))).scalar_one_or_none()
    by_username = (await db.execute(base.where(func.lower(User.username) == ident))).scalar_one_or_none()
    if by_username is not None:
        return by_username
    local_part = func.lower(func.split_part(User.email, "@", 1))
    local = (await db.execute(base.where(local_part == ident).limit(2))).scalars().all()
    return local[0] if len(local) == 1 else None
