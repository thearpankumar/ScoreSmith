"""Worker drain (SIGTERM) semantics: stop claiming, let short turns finish, interrupt long ones so the user is
offered a retry, and release evaluation leases so a peer takes over at once. Also the chat-claim capacity cap."""

from __future__ import annotations

import asyncio

import pytest

from app.api.v1 import chat as chat_mod
from app.config import get_settings
from tests.ai_eval_helpers import seed_scorecard, wait_until
from tests.test_scaling_leases import _chat_row, _cleanup, _queued_turn, fake_turn  # noqa: F401


async def test_stop_claiming_leaves_queued_turns_for_a_peer(async_db_session, fake_turn) -> None:  # noqa: F811
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    draining, healthy = chat_mod.ChatTurnRunner(), chat_mod.ChatTurnRunner()
    draining.stop_claiming()
    assert await draining.claim() == 0
    assert (await _chat_row(session.id)).turn_lease_owner is None  # untouched
    assert await healthy.claim() == 1  # a live peer picks it up
    await wait_until(lambda: fake_turn == [session.id])


async def test_stop_interrupts_running_turns_and_clears_their_markers(async_db_session, fake_turn) -> None:  # noqa: F811
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    runner = chat_mod.ChatTurnRunner()
    assert await runner.claim() == 1
    await wait_until(lambda: bool(fake_turn))
    await runner.stop()  # grace period over: the turn is cancelled and recorded as interrupted
    row = await _chat_row(session.id)
    assert row.last_turn_error_code == "interrupted"
    assert row.pending_turn_started_at is None and row.turn_lease_owner is None and row.turn_message is None
    assert runner.running() == 0


async def test_a_stopped_runner_does_not_claim_again(async_db_session, fake_turn) -> None:  # noqa: F811
    owner, *_ = await seed_scorecard(async_db_session)
    runner = chat_mod.ChatTurnRunner()
    await runner.stop()
    await _queued_turn(async_db_session, owner.id)
    assert await runner.claim() == 0


async def test_claim_respects_the_per_process_chat_capacity(async_db_session, fake_turn, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "chat_turn_max_concurrent", 2)
    owner, *_ = await seed_scorecard(async_db_session)
    sessions = [await _queued_turn(async_db_session, owner.id) for _ in range(5)]
    first, second = chat_mod.ChatTurnRunner(), chat_mod.ChatTurnRunner()
    assert await first.claim() <= 2
    await wait_until(lambda: len(fake_turn) >= 1)
    # `running()` counts this process's tasks, so the cap is per process: a second runner in the SAME process
    # sees the same task table and must not exceed it either.
    await second.claim()
    assert first.running() <= 2
    await asyncio.sleep(0.1)
    assert len(set(fake_turn)) == len(fake_turn) <= 2
    queued_left = [s for s in sessions if (await _chat_row(s.id)).turn_lease_owner is None]
    assert len(queued_left) >= 3
    await first.stop()


async def test_a_cancel_flag_on_a_never_claimed_turn_is_not_claimed(async_db_session, fake_turn) -> None:  # noqa: F811
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.db import AsyncSessionLocal
    from app.models.chat_session import ChatSession

    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ChatSession).where(ChatSession.id == session.id).values(turn_cancel_requested_at=datetime.now(UTC))
        )
        await db.commit()
    assert await chat_mod.ChatTurnRunner().claim() == 0  # the user already cancelled it: never start it
    assert fake_turn == []


@pytest.mark.parametrize("worker_count", [2, 4])
async def test_many_runners_start_each_queued_turn_exactly_once(async_db_session, fake_turn, worker_count) -> None:  # noqa: F811
    owner, *_ = await seed_scorecard(async_db_session)
    sessions = [await _queued_turn(async_db_session, owner.id) for _ in range(3)]
    runners = [chat_mod.ChatTurnRunner() for _ in range(worker_count)]
    await asyncio.gather(*(r.claim() for r in runners for _ in range(2)))
    await wait_until(lambda: len(fake_turn) == 3)
    await asyncio.sleep(0.1)
    assert sorted(fake_turn, key=str) == sorted((s.id for s in sessions), key=str)
    assert len(set(fake_turn)) == len(fake_turn)
