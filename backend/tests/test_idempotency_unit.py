"""Idempotency-Key store semantics against real Postgres (app/idempotency.py)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import idempotency as idem
from app.db import AsyncSessionLocal
from app.models.idempotency_key import IdempotencyKey
from app.models.user import User


async def _user(db) -> uuid.UUID:
    u = User(email=f"{uuid.uuid4().hex[:8]}@example.com", name="U")
    db.add(u)
    await db.commit()
    return u.id


def test_normalize_key_trims_blank_means_none_and_length_is_capped() -> None:
    assert idem.normalize_key(None) is None
    assert idem.normalize_key("") is None
    assert idem.normalize_key("   \t") is None
    assert idem.normalize_key("  abc  ") == "abc"
    assert idem.normalize_key("k" * idem.MAX_KEY_LENGTH) == "k" * idem.MAX_KEY_LENGTH
    with pytest.raises(HTTPException) as err:
        idem.normalize_key("k" * (idem.MAX_KEY_LENGTH + 1))
    assert err.value.status_code == 422


def test_fingerprint_is_stable_order_sensitive_and_separator_safe() -> None:
    assert idem.request_fingerprint("a", 1) == idem.request_fingerprint("a", 1)
    assert idem.request_fingerprint("a", "b") != idem.request_fingerprint("b", "a")
    assert idem.request_fingerprint("ab", "c") != idem.request_fingerprint("a", "bc")
    assert len(idem.request_fingerprint()) == 64


async def test_first_use_reserves_then_replay_returns_the_stored_result(async_db_session) -> None:
    db = async_db_session
    uid = await _user(db)
    assert await idem.begin(db, uid, "ai_jobs", "k1", "h1") is None
    await idem.remember(db, uid, "ai_jobs", "k1", {"evaluation_ids": ["a", "b"]})
    await db.commit()
    assert await idem.begin(db, uid, "ai_jobs", "k1", "h1") == {"evaluation_ids": ["a", "b"]}
    await db.commit()
    assert len((await db.execute(select(IdempotencyKey))).scalars().all()) == 1


async def test_same_key_with_a_different_request_is_422_and_changes_nothing(async_db_session) -> None:
    db = async_db_session
    uid = await _user(db)
    await idem.begin(db, uid, "ai_jobs", "k", "h1")
    await idem.remember(db, uid, "ai_jobs", "k", {"ok": True})
    await db.commit()
    with pytest.raises(HTTPException) as err:
        await idem.begin(db, uid, "ai_jobs", "k", "OTHER")
    assert err.value.status_code == 422
    await db.rollback()
    row = (await db.execute(select(IdempotencyKey))).scalar_one()
    assert row.request_hash == "h1" and row.result == {"ok": True}


async def test_a_reserved_but_unfinished_key_is_409_in_flight(async_db_session) -> None:
    db = async_db_session
    uid = await _user(db)
    await idem.begin(db, uid, "ai_jobs", "k", "h")
    await db.commit()  # reservation committed, result never stored (e.g. crash between the two)
    with pytest.raises(HTTPException) as err:
        await idem.begin(db, uid, "ai_jobs", "k", "h")
    assert err.value.status_code == 409


async def test_keys_are_scoped_by_user_and_by_scope(async_db_session) -> None:
    db = async_db_session
    a, b = await _user(db), await _user(db)
    assert await idem.begin(db, a, "ai_jobs", "same", "h") is None
    assert await idem.begin(db, b, "ai_jobs", "same", "h") is None  # another user: independent
    assert await idem.begin(db, a, "evaluations", "same", "h") is None  # another scope: independent
    await db.commit()
    assert len((await db.execute(select(IdempotencyKey))).scalars().all()) == 3


async def test_a_rolled_back_request_leaves_no_key_behind(async_db_session) -> None:
    db = async_db_session
    uid = await _user(db)
    await idem.begin(db, uid, "ai_jobs", "k", "h")
    await db.rollback()  # the request failed before commit
    assert await idem.begin(db, uid, "ai_jobs", "k", "h") is None  # the client may simply retry


async def test_two_concurrent_requests_with_one_key_serialise_on_the_unique_index(async_db_session) -> None:
    uid = await _user(async_db_session)
    first_has_key = asyncio.Event()
    outcomes: dict[str, object] = {}

    async def first() -> None:
        async with AsyncSessionLocal() as db:
            outcomes["first"] = await idem.begin(db, uid, "ai_jobs", "race", "h")
            first_has_key.set()
            await asyncio.sleep(0.4)  # "creating rows" while holding the reservation
            await idem.remember(db, uid, "ai_jobs", "race", {"evaluation_ids": ["x"]})
            await db.commit()

    async def second() -> None:
        await first_has_key.wait()
        async with AsyncSessionLocal() as db:
            outcomes["second"] = await idem.begin(db, uid, "ai_jobs", "race", "h")  # blocks until first commits
            await db.commit()

    await asyncio.gather(first(), second())
    assert outcomes["first"] is None  # the first caller does the work...
    assert outcomes["second"] == {"evaluation_ids": ["x"]}  # ...the second sees its stored result, no second job
