"""Trash <-> running jobs: jobs cancelled BECAUSE the chart was trashed (`cancel_reason = cancelled_by_trash`) are
re-queued by a restore - exactly those, once, respecting the one-job-per-user slot; a purge never resurrects
anything."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import trash
from app.main import app
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.scorecard import Scorecard
from app.models.sharing import ScorecardActivity
from app.pipeline.dispatcher import get_dispatcher
from tests.fakes import FakeAwsJobs, default_corpus
from tests.sharing_world import Team, hdr, make_chart, team  # noqa: F401 - fixture
from tests.test_chart_trash import TRASH, _backdate, _inbox_types, _run_purge, _trash
from tests.test_evaluations_ai_api import aws  # noqa: F401 - fixture (fake AWS + dispatcher override)
from tests.test_user_slots import _items, _post


def _row(db: Session, eid: str) -> Evaluation:
    db.expire_all()
    return db.get(Evaluation, uuid.UUID(eid))


def _restore(client: TestClient, t: Team) -> None:
    r = client.post(f"{TRASH}/restore", json={"ids": [t.sc]}, headers=hdr(t.owner))
    assert r.status_code == 200, r.text


def _drive_to_idle(fake: FakeAwsJobs, db: Session) -> None:
    """Lets the fake pipeline finish every non-terminal evaluation, then runs the dispatcher until idle."""
    fake.hold = False
    db.expire_all()
    for eid in db.execute(select(Evaluation.id)).scalars():
        fake.json_objects[f"derived/{eid}/corpus.json"] = default_corpus(str(eid))
    asyncio.run(app.dependency_overrides[get_dispatcher]().run_until_idle())  # the fixture's fake-backed dispatcher


def test_trash_while_running_then_restore_requeues_and_the_job_completes(
    client: TestClient, db_session: Session, team: Team, aws  # noqa: F811
) -> None:
    team.share(client)
    mine = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    theirs = _post(client, team.invitee, team.sc, _items()).json()["evaluations"][0]["id"]
    _trash(client, team)
    for eid in (mine, theirs):
        ev = _row(db_session, eid)
        assert ev.status == EvaluationStatus.FAILED and ev.error_code == "cancelled"
        assert ev.cancel_reason == trash.CANCELLED_BY_TRASH

    _restore(client, team)

    for eid in (mine, theirs):
        ev = _row(db_session, eid)
        assert ev.status == EvaluationStatus.QUEUED and ev.attempt == 2 and ev.error_code is None
        assert ev.cancel_reason == trash.TRASH_RESTORE_HANDLED and ev.lease_owner is None
    # each runner is told, and the chart's log records it
    assert "evaluation_resumed" in _inbox_types(client, team.owner)
    assert "evaluation_resumed" in _inbox_types(client, team.invitee)
    actions = [a.action for a in db_session.execute(select(ScorecardActivity)).scalars()]
    assert actions.count("evaluations_resumed") == 2 and "chart_restored" in actions

    _drive_to_idle(aws, db_session)
    for eid in (mine, theirs):
        assert _row(db_session, eid).status == EvaluationStatus.COMPLETED


def test_a_batch_comes_back_whole_and_running(client: TestClient, db_session: Session, team: Team, aws) -> None:  # noqa: F811
    batch = _post(client, team.owner, team.sc, _items(3)).json()
    ids = [e["id"] for e in batch["evaluations"]]
    _trash(client, team)
    _restore(client, team)
    assert [_row(db_session, i).status for i in ids] == [EvaluationStatus.QUEUED] * 3
    _drive_to_idle(aws, db_session)
    assert [_row(db_session, i).status for i in ids] == [EvaluationStatus.COMPLETED] * 3


def test_a_runner_with_another_active_job_is_offered_a_retry_instead_of_a_second_slot(
    client: TestClient, db_session: Session, team: Team, aws  # noqa: F811
) -> None:
    stopped = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    _trash(client, team)
    other = make_chart(db_session, team.owner, "Other chart")  # the owner starts something else meanwhile
    busy = _post(client, team.owner, other["id"], _items(1, "z"))
    assert busy.status_code == 202, busy.text

    _restore(client, team)

    ev = _row(db_session, stopped)
    assert ev.status == EvaluationStatus.FAILED and ev.cancel_reason == trash.TRASH_RESTORE_HANDLED  # NOT queued
    types = _inbox_types(client, team.owner)
    assert "evaluation_retry_available" in types and "evaluation_resumed" not in types
    active = db_session.execute(
        select(Evaluation).where(Evaluation.owner_id == team.owner.id, Evaluation.status == EvaluationStatus.QUEUED)
    ).scalars().all()
    assert len(active) == 1  # still exactly one job for the user
    retry = client.post(f"/api/v1/evaluations/{stopped}/retry", headers=hdr(team.owner))
    assert retry.status_code == 409 and retry.json()["detail"]["code"] == "user_job_active"


def test_only_trash_cancelled_jobs_are_resumed_and_never_twice(
    client: TestClient, db_session: Session, team: Team, aws  # noqa: F811
) -> None:
    by_user = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    assert client.post(f"/api/v1/evaluations/{by_user}/cancel", headers=hdr(team.owner)).status_code == 202
    by_trash = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    _trash(client, team)
    assert _row(db_session, by_user).cancel_reason is None
    _restore(client, team)
    assert _row(db_session, by_user).status == EvaluationStatus.FAILED  # the user's own cancel stays cancelled
    assert _row(db_session, by_trash).status == EvaluationStatus.QUEUED

    _drive_to_idle(aws, db_session)
    assert _row(db_session, by_trash).status == EvaluationStatus.COMPLETED
    _trash(client, team)
    _restore(client, team)
    assert _row(db_session, by_trash).status == EvaluationStatus.COMPLETED  # finished jobs are never touched
    assert _row(db_session, by_user).status == EvaluationStatus.FAILED
    assert _row(db_session, by_trash).attempt == 2


def test_a_deactivated_runner_is_not_resumed(client: TestClient, db_session: Session, team: Team, aws) -> None:  # noqa: F811
    team.share(client)
    theirs = _post(client, team.invitee, team.sc, _items()).json()["evaluations"][0]["id"]
    _trash(client, team)
    team.invitee.is_active = False
    db_session.commit()
    _restore(client, team)
    ev = _row(db_session, theirs)
    assert ev.status == EvaluationStatus.FAILED and ev.cancel_reason == trash.TRASH_RESTORE_HANDLED


def test_purge_after_retention_does_not_resurrect_cancelled_jobs(
    client: TestClient, db_session: Session, team: Team, aws  # noqa: F811
) -> None:
    eid = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    _trash(client, team)
    _backdate(db_session, team.sc, 31)
    assert _run_purge() == 1
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)) is None
    assert db_session.get(Evaluation, uuid.UUID(eid)) is None  # gone with the chart, nothing queued anywhere
    _drive_to_idle(aws, db_session)  # the dispatcher finds nothing to run
    assert db_session.execute(select(Evaluation)).scalars().all() == []
    # a chart trashed 29 days ago is still inside the retention window and is kept (restorable)
    other = make_chart(db_session, team.owner, "Boundary")
    assert client.delete(f"/api/v1/scorecards/{other['id']}", headers=hdr(team.owner)).status_code == 204
    _backdate(db_session, other["id"], 29)
    assert _run_purge(datetime.now(UTC) + timedelta(hours=1)) == 0
