"""Horizontal scaling: leases, adoption, cross-process cancel flags and transient-failure retries.

"Two processes" are two `Dispatcher` / `ChatTurnRunner` objects sharing one real Postgres: they have separate
worker ids and separate in-memory task tables, and can only influence each other through the database -
exactly the relationship two containers have.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.ai.bedrock_client import BedrockUnavailableError
from app.api.v1 import chat as chat_mod
from app.db import AsyncSessionLocal
from app.models.chat_session import ChatSession
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_event import EvaluationEvent
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.pipeline.dispatcher import Dispatcher
from tests.ai_eval_helpers import (
    get_eval,
    seed_queued,
    seed_scorecard,
    wait_execution_recorded,
    wait_status,
    wait_until,
)
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, default_corpus, master_converse_fn

_CREATED: list[Dispatcher] = []


def make_dispatcher(aws, *, bedrock=None, jev=None, limit=3, retry_delays=()):
    d = Dispatcher(
        aws,
        bedrock or FakeBedrockClient(converse_fn=master_converse_fn()),
        jev if jev is not None else FakeJevScoreClient(),
        max_concurrent=limit,
        poll_seconds=0.01,
        jev_retry_delays=(0.0, 0.0),
        patience_waits=(),
        score_retry_delays=retry_delays,
    )
    _CREATED.append(d)
    return d


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    while _CREATED:
        d = _CREATED.pop()
        pending = [t for t in d._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        await d.stop()
    await chat_mod.get_turn_runner().stop()


async def _set_lease_expiry(eid, seconds_from_now: float) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Evaluation)
            .where(Evaluation.id == eid)
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=seconds_from_now))
        )
        await db.commit()


async def _until_adopted(d: Dispatcher, eid, *, via: str = "recover", timeout: float = 30.0) -> int:
    """Adoption uses `FOR UPDATE SKIP LOCKED`, so a row whose previous owner's cancelled transaction is still being
    rolled back is skipped for a moment (correct behaviour). Poll the adopting call until it takes the row instead of
    assuming the first call lands after that rollback; returns what the successful call returned."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await d.recover() if via == "recover" else len(await d.tick())
        if (await get_eval(eid)).lease_owner == d.worker_id:
            return result
        assert asyncio.get_running_loop().time() < deadline, "the expired lease was never adopted"
        await asyncio.sleep(0.05)


async def _kill(d: Dispatcher) -> None:
    """kill -9: the drivers vanish and NOTHING is released or cleaned up."""
    tasks = list(d._tasks.values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- evaluation leases -------------------------------------------------------------------------------------


async def test_claim_stamps_a_lease_and_a_live_lease_is_never_adopted_only_an_expired_one(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d1, d2 = make_dispatcher(aws), make_dispatcher(aws)

    await d1.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    row = await wait_execution_recorded(ev.id)
    assert row.lease_owner == d1.worker_id and row.heartbeat_at is not None
    assert row.lease_expires_at > datetime.now(UTC) + timedelta(seconds=30)

    await _kill(d1)  # the owner dies without releasing anything

    # Its lease is still valid: another process must NOT drive the evaluation (that would double the spend).
    assert await d2.recover() == 0
    await d2.tick()
    assert not d2._tasks and (await get_eval(ev.id)).lease_owner == d1.worker_id

    await _set_lease_expiry(ev.id, -1)  # ... and now it has expired
    assert await _until_adopted(d2, ev.id) == 1
    assert (await get_eval(ev.id)).lease_owner == d2.worker_id
    events = await _events(ev.id)
    assert "resumed" in events

    # d2 resumed the existing execution (by ARN) rather than starting a second one.
    aws.finish(str(ev.id))
    done = await wait_status(ev.id, EvaluationStatus.COMPLETED)
    assert aws.start_names == [str(ev.id)] and done.lease_owner is None and done.lease_expires_at is None


async def test_expired_lease_is_adopted_by_a_running_peer_on_its_next_tick(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d1, d2 = make_dispatcher(aws), make_dispatcher(aws)
    await d1.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    await wait_execution_recorded(ev.id)  # the crash happens AFTER the execution was recorded (else a restart is right)
    await _kill(d1)
    await _set_lease_expiry(ev.id, -1)

    await _until_adopted(d2, ev.id, via="tick")  # no restart needed: the periodic claim also adopts expired leases
    assert (await get_eval(ev.id)).lease_owner == d2.worker_id and ev.id in d2._tasks
    aws.finish(str(ev.id))  # the survivor resumes the recorded execution, so one finish() is enough
    await wait_status(ev.id, EvaluationStatus.COMPLETED, timeout=30)
    assert aws.start_names == [str(ev.id)]


async def test_heartbeat_renews_the_lease_and_a_driver_that_lost_its_lease_stops(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d1 = make_dispatcher(aws)
    await d1.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    await wait_execution_recorded(ev.id)

    await _set_lease_expiry(ev.id, 3)
    assert await d1.heartbeat() == 1
    assert (await get_eval(ev.id)).lease_expires_at > datetime.now(UTC) + timedelta(seconds=30)

    # Another worker adopted it while this process was stalled: the local driver must stop at the next beat.
    async with AsyncSessionLocal() as db:
        await db.execute(update(Evaluation).where(Evaluation.id == ev.id).values(lease_owner="someone-else"))
        await db.commit()
    task = d1._tasks[ev.id]
    assert await d1.heartbeat() == 0
    await asyncio.wait([task], timeout=5)
    assert task.cancelled()
    assert (await get_eval(ev.id)).status == EvaluationStatus.INGESTING  # untouched: the new owner drives it


async def test_clean_shutdown_releases_leases_so_a_peer_adopts_immediately(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d1, d2 = make_dispatcher(aws), make_dispatcher(aws)
    await d1.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    await wait_execution_recorded(ev.id)
    await d1.stop()  # SIGTERM drain
    row = await get_eval(ev.id)
    assert row.lease_owner is None and row.lease_expires_at is None and row.status == EvaluationStatus.INGESTING
    assert await _until_adopted(d2, ev.id) == 1  # no waiting for a lease to expire


# --- cancel is a DB flag, effective across processes ---------------------------------------------------------


async def test_cancel_from_another_process_stops_the_driver_on_this_one(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    worker, api = make_dispatcher(aws), make_dispatcher(aws)
    await worker.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    assert ev.id in worker._tasks and not api._tasks  # the API process has no driver for it

    await api.cancel(ev.id)

    await wait_until(lambda: not worker._tasks)  # the worker's driver noticed and ended on its own
    row = await get_eval(ev.id)
    assert row.status == EvaluationStatus.FAILED and row.error_code == "cancelled"
    assert row.cancel_requested_at is not None and row.lease_owner is None
    assert len(aws.stopped) == 1


async def test_cancel_flag_alone_is_enough_for_the_driver_to_finish_the_cancellation(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    worker = make_dispatcher(aws)
    await worker.tick()
    await wait_until(lambda: len(aws.start_names) == 1)

    async with AsyncSessionLocal() as db:  # some other process only sets the flag
        await db.execute(update(Evaluation).where(Evaluation.id == ev.id).values(cancel_requested_at=datetime.now(UTC)))
        await db.commit()

    row = await wait_status(ev.id, EvaluationStatus.FAILED)
    assert row.error_code == "cancelled" and row.stage == "cancelled" and len(aws.stopped) == 1
    await wait_until(lambda: not worker._tasks)


async def test_scoring_stops_between_kpis_when_the_cancel_flag_is_set(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1, status=EvaluationStatus.SCORING)
    async with AsyncSessionLocal() as db:
        await db.execute(update(Evaluation).where(Evaluation.id == ev.id).values(cancel_requested_at=datetime.now(UTC)))
        await db.commit()
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = default_corpus(str(ev.id))
    d = make_dispatcher(aws)
    await d.recover()  # adopts the SCORING row and drives its scoring graph

    row = await wait_status(ev.id, EvaluationStatus.FAILED)
    assert row.error_code == "cancelled", row.error_message
    async with AsyncSessionLocal() as db:
        results = (
            await db.execute(select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == ev.id))
        ).scalars().all()
    assert results == []  # no KPI was scored after the cancel


# --- transient scoring failures are retried ------------------------------------------------------------------


async def _events(eid) -> list[str]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(EvaluationEvent.event_type).where(EvaluationEvent.evaluation_id == eid))).all()
    return [r[0] for r in rows]


async def test_transient_model_outage_is_retried_up_to_twice_then_fails(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)

    def down(**_kw):
        raise BedrockUnavailableError("bedrock down")

    d = make_dispatcher(
        FakeAwsJobs(), jev=FakeJevScoreClient(fail=True), bedrock=FakeBedrockClient(converse_fn=down),
        retry_delays=(0.0, 0.0),
    )
    await d.run_until_idle()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.FAILED and done.error_code == "scoring_failed"
    assert (await _events(ev.id)).count("scoring_retry") == 2


async def test_scoring_recovers_when_the_outage_was_temporary(async_db_session) -> None:
    from app.ai.jev_client import JevUnavailableError

    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    healthy_fn = master_converse_fn()
    state = {"healthy": False}

    def flaky(**kw):
        if not state["healthy"]:
            raise BedrockUnavailableError("bedrock down")
        return healthy_fn(**kw)

    class FlakyJev(FakeJevScoreClient):
        async def score(self, **kw):
            if not state["healthy"]:
                raise JevUnavailableError("jev down")
            return await super().score(**kw)

    async def heal() -> None:  # the outage ends while scoring waits to retry
        await wait_until(lambda: _has_event(ev.id, "scoring_retry"))
        state["healthy"] = True

    d = make_dispatcher(
        FakeAwsJobs(), jev=FlakyJev(), bedrock=FakeBedrockClient(converse_fn=flaky), retry_delays=(0.3, 0.3)
    )
    await asyncio.gather(d.run_until_idle(), heal())
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert (await _events(ev.id)).count("scoring_retry") >= 1


async def _has_event(eid, event_type: str) -> bool:
    return event_type in await _events(eid)


async def test_non_transient_failures_are_not_retried(async_db_session) -> None:
    from app.pipeline.aws_jobs import AwsNotConfiguredError

    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.fail_start = AwsNotConfiguredError("SFN_STATE_MACHINE_ARN is not configured.")
    await make_dispatcher(aws, retry_delays=(0.0, 0.0)).run_until_idle()
    assert (await get_eval(ev.id)).error_code == "internal"
    assert "scoring_retry" not in await _events(ev.id)


# --- chat turns: SKIP LOCKED claim, lease, cross-process cancel ------------------------------------------------


async def _queued_turn(db, owner_id, *, lease_owner: str | None = None, expires_in: float = 60) -> ChatSession:
    now = datetime.now(UTC)
    session = ChatSession(
        user_id=owner_id, pending_turn_started_at=now, turn_message="hello", turn_first=True,
        turn_lease_owner=lease_owner,
        turn_lease_expires_at=(now + timedelta(seconds=expires_in)) if lease_owner else None,
    )
    db.add(session)
    await db.commit()
    return session


async def _chat_row(sid) -> ChatSession:
    async with AsyncSessionLocal() as db:
        return await db.get(ChatSession, sid)


@pytest.fixture()
def fake_turn(monkeypatch):
    """Stands in for the LangGraph turn: runs until cancelled and, like the real one, records why it ended."""
    started: list[uuid.UUID] = []

    async def _turn(*, session_id, turn_started_at, **_kw):
        started.append(session_id)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            code = "cancelled" if session_id in chat_mod._user_cancelled else "interrupted"
            chat_mod._user_cancelled.discard(session_id)
            await asyncio.shield(chat_mod._record_turn_failure(session_id, turn_started_at, code, "stopped"))
            raise

    monkeypatch.setattr(chat_mod, "_background_turn", _turn)
    return started


async def test_two_runners_claim_a_queued_turn_exactly_once(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    r1, r2 = chat_mod.ChatTurnRunner(), chat_mod.ChatTurnRunner()

    claimed = await asyncio.gather(r1.claim(), r2.claim(), r1.claim(), r2.claim())
    assert sum(claimed) == 1
    await asyncio.sleep(0.05)
    assert fake_turn == [session.id]
    assert (await _chat_row(session.id)).turn_lease_owner in {r1.worker_id, r2.worker_id}


async def test_runner_does_not_claim_turns_a_live_worker_holds_or_inline_markers(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    await _queued_turn(async_db_session, owner.id, lease_owner="other-worker")
    inline = ChatSession(user_id=owner.id, pending_turn_started_at=datetime.now(UTC))  # no queued job
    async_db_session.add(inline)
    await async_db_session.commit()
    assert await chat_mod.ChatTurnRunner().claim() == 0


async def test_chat_cancel_flag_set_by_another_process_stops_the_turn_here(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    worker = chat_mod.ChatTurnRunner()
    assert await worker.claim() == 1
    await wait_until(lambda: bool(fake_turn))

    async with AsyncSessionLocal() as db:  # what the API container does: only flips the flag
        await db.execute(
            update(ChatSession).where(ChatSession.id == session.id).values(turn_cancel_requested_at=datetime.now(UTC))
        )
        await db.commit()
    await worker.supervise_once()

    await wait_until(lambda: session.id not in chat_mod._turn_tasks)
    row = await _chat_row(session.id)
    assert row.pending_turn_started_at is None and row.last_turn_error_code == "cancelled"
    assert row.turn_lease_owner is None and row.turn_message is None and row.turn_cancel_requested_at is None


async def test_chat_turn_of_a_deleted_session_is_stopped_by_the_supervisor(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    worker = chat_mod.ChatTurnRunner()
    await worker.claim()
    await wait_until(lambda: bool(fake_turn))
    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM chat_sessions WHERE id = :i"), {"i": session.id})
        await db.commit()
    task = chat_mod._turn_tasks[session.id]
    await worker.supervise_once()
    await asyncio.wait([task], timeout=5)
    assert task.cancelled()


async def test_chat_supervisor_renews_the_lease(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    worker = chat_mod.ChatTurnRunner()
    await worker.claim()
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ChatSession).where(ChatSession.id == session.id)
            .values(turn_lease_expires_at=datetime.now(UTC) + timedelta(seconds=2))
        )
        await db.commit()
    await worker.supervise_once()
    assert (await _chat_row(session.id)).turn_lease_expires_at > datetime.now(UTC) + timedelta(seconds=30)


async def test_expired_chat_lease_is_reaped_live_and_queued_ones_are_not(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    dead = await _queued_turn(async_db_session, owner.id, lease_owner="dead", expires_in=-5)
    alive = await _queued_turn(async_db_session, owner.id, lease_owner="alive", expires_in=300)
    queued = await _queued_turn(async_db_session, owner.id)
    assert await chat_mod.ChatTurnRunner().reap() == 1
    assert (await _chat_row(dead.id)).last_turn_error_code == "interrupted"
    assert (await _chat_row(alive.id)).pending_turn_started_at is not None
    assert (await _chat_row(queued.id)).pending_turn_started_at is not None


async def test_cancelling_a_still_queued_turn_records_it_without_any_worker(async_db_session, fake_turn) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    assert await chat_mod.cancel_background_turn(session.id) is True
    row = await _chat_row(session.id)
    assert row.pending_turn_started_at is None and row.last_turn_error_code == "cancelled"
    assert await chat_mod.cancel_background_turn(session.id) is False  # nothing left to cancel
