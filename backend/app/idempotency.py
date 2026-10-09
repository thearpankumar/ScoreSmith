"""`Idempotency-Key` support for the evaluation / batch creation POSTs.

A client that retries a POST after a timeout (or a double-clicked button) must not queue - and pay for -
the same evaluation twice. With the header present:

    stored = await begin(db, user_id, "ai_jobs", key, request_hash)   # None: first time, key now reserved
    ...create rows...
    await remember(db, user_id, "ai_jobs", key, {"evaluation_ids": [...]})
    await db.commit()                                                  # key + rows commit atomically

The reservation is an `INSERT .. ON CONFLICT DO NOTHING` inside the caller's transaction, so two concurrent
requests with one key serialise on the unique index: the second waits for the first to commit, then sees its
stored result. A request that fails before committing leaves no key behind (the client may retry freely).
Without the header nothing changes.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_key import IdempotencyKey

MAX_KEY_LENGTH = 200


def request_fingerprint(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def normalize_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > MAX_KEY_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Idempotency-Key is longer than {MAX_KEY_LENGTH} characters."
        )
    return key


async def begin(
    db: AsyncSession, user_id: uuid.UUID, scope: str, key: str, request_hash: str
) -> dict[str, Any] | None:
    """Reserves `key`. Returns None when this is its first use, or the stored result of the earlier request."""
    inserted = (
        await db.execute(
            pg_insert(IdempotencyKey)
            .values(user_id=user_id, scope=scope, key=key, request_hash=request_hash)
            .on_conflict_do_nothing()
            .returning(IdempotencyKey.key)
        )
    ).first()
    if inserted is not None:
        return None
    row = (
        await db.execute(
            select(IdempotencyKey.request_hash, IdempotencyKey.result).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.scope == scope, IdempotencyKey.key == key
            )
        )
    ).first()
    if row is None:  # deleted between the insert and the read: treat as in flight
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A request with this Idempotency-Key is in progress.")
    stored_hash, result = row
    if stored_hash != request_hash:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This Idempotency-Key was already used with a different request.",
        )
    if result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A request with this Idempotency-Key is in progress.")
    return result


async def remember(db: AsyncSession, user_id: uuid.UUID, scope: str, key: str, result: dict[str, Any]) -> None:
    await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.user_id == user_id, IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .values(result=result)
    )
