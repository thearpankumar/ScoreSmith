"""Notification inbox: per-user scoping, keyset pagination, read state, ETag polling, idempotent creation (also from the
worker: a re-driven evaluation announces itself once) and the worker-created event types."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notifications as notif
from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus
from app.models.notification import Notification
from app.pipeline.dispatcher import Dispatcher
from app.pipeline.outcomes import finalize_evaluation_background
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn
from tests.sharing_world import Team, hdr, team  # noqa: F401 - fixture


def _seed(db: Session, user, n: int, *, prefix: str = "n") -> list[Notification]:
    base = datetime.now(UTC) - timedelta(hours=1)
    rows = [
        Notification(user_id=user.id, type="chart_edited", title=f"{prefix}{i}", created_at=base + timedelta(seconds=i))
        for i in range(n)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def test_inbox_is_scoped_to_its_owner_and_paginates_without_gaps(client: TestClient, db_session: Session, team: Team) -> None:
    _seed(db_session, team.owner, 23, prefix="mine")
    _seed(db_session, team.invitee, 4, prefix="theirs")
    h = hdr(team.owner)
    seen, cursor = [], None
    while True:
        url = "/api/v1/notifications?limit=10" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=h).json()
        seen += [n["title"] for n in page["items"]]
        assert page["unread"] == 23
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"mine{i}" for i in reversed(range(23))]  # newest first, no duplicates, no gaps
    assert client.get("/api/v1/notifications?cursor=garbage", headers=h).status_code == 422
    assert all(n["title"].startswith("theirs") for n in client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"])


def test_mark_read_mark_all_and_cross_user_isolation(client: TestClient, db_session: Session, team: Team) -> None:
    mine = _seed(db_session, team.owner, 3)
    theirs = _seed(db_session, team.invitee, 2)
    h = hdr(team.owner)
    assert client.get("/api/v1/notifications/unread-count", headers=h).json() == {"unread": 3}
    assert client.post(f"/api/v1/notifications/{mine[0].id}/read", headers=h).json() == {"unread": 2}
    assert client.post(f"/api/v1/notifications/{mine[0].id}/read", headers=h).json() == {"unread": 2}  # idempotent
    # someone else's notification is a 404 and stays unread
    assert client.post(f"/api/v1/notifications/{theirs[0].id}/read", headers=h).status_code == 404
    assert client.post(f"/api/v1/notifications/{uuid.uuid4()}/read", headers=h).status_code == 404
    assert client.post("/api/v1/notifications/read-all", headers=h).json() == {"unread": 0}
    assert client.get("/api/v1/notifications/unread-count", headers=hdr(team.invitee)).json() == {"unread": 2}
    assert client.get("/api/v1/notifications?unread_only=true", headers=h).json()["items"] == []
    assert len(client.get("/api/v1/notifications", headers=h).json()["items"]) == 3  # read ones stay listed


def test_unread_count_supports_conditional_requests(client: TestClient, db_session: Session, team: Team) -> None:
    h = hdr(team.owner)
    first = client.get("/api/v1/notifications/unread-count", headers=h)
    etag = first.headers["etag"]
    assert first.json() == {"unread": 0}
    same = client.get("/api/v1/notifications/unread-count", headers={**h, "If-None-Match": etag})
    assert same.status_code == 304 and same.content == b""
    _seed(db_session, team.owner, 1)
    changed = client.get("/api/v1/notifications/unread-count", headers={**h, "If-None-Match": etag})
    assert changed.status_code == 200 and changed.json() == {"unread": 1} and changed.headers["etag"] != etag
    # reading it changes the tag again (the badge must drop)
    n = db_session.scalars(select(Notification)).one()
    client.post(f"/api/v1/notifications/{n.id}/read", headers=h)
    after = client.get("/api/v1/notifications/unread-count", headers={**h, "If-None-Match": changed.headers["etag"]})
    assert after.status_code == 200 and after.json() == {"unread": 0}


async def test_creation_is_idempotent_per_user_and_key(async_db_session) -> None:
    from app.models.user import User

    a, b = User(email="a@example.com", name="A"), User(email="b@example.com", name="B")
    async_db_session.add_all([a, b])
    await async_db_session.commit()
    assert await notif.add_notification(async_db_session, a.id, "x", "t", dedupe_key="k1") is True
    assert await notif.add_notification(async_db_session, a.id, "x", "t", dedupe_key="k1") is False
    assert await notif.add_notification(async_db_session, b.id, "x", "t", dedupe_key="k1") is True  # per user
    assert await notif.add_notification(async_db_session, a.id, "x", "t") is True  # no key: never deduplicated
    assert await notif.add_notification(async_db_session, a.id, "x", "t") is True
    await async_db_session.commit()
    rows = (await async_db_session.execute(select(Notification))).scalars().all()
    assert len(rows) == 4


async def test_a_finished_evaluation_notifies_its_runner_exactly_once(async_db_session) -> None:
    owner, _sc, version, _nodes = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    d = Dispatcher(
        FakeAwsJobs(), FakeBedrockClient(converse_fn=master_converse_fn()), FakeJevScoreClient(),
        max_concurrent=2, poll_seconds=0.01, jev_retry_delays=(0.0, 0.0), patience_waits=(),
    )
    await d.run_until_idle()
    assert (await get_eval(ev.id)).status == EvaluationStatus.COMPLETED
    # a re-driven job (worker crash + adoption) runs the finaliser again: still one notification, one log line
    await finalize_evaluation_background(ev.id)
    await finalize_evaluation_background(ev.id)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalars().all()
        from app.models.sharing import ScorecardActivity

        log = (await db.execute(select(ScorecardActivity).where(ScorecardActivity.action == "evaluation_completed"))).scalars().all()
    assert [(n.type, n.data["evaluation_id"]) for n in rows] == [("evaluation_completed", str(ev.id))]
    assert "Score" in rows[0].body and rows[0].link.endswith(str(ev.id))
    assert len(log) == 1 and log[0].actor_id == owner.id


async def test_batch_members_are_summarised_by_one_batch_notification(async_db_session) -> None:
    from app.models.evaluation import Evaluation
    from app.models.evaluation_batch import EvaluationBatch

    owner, sc, version, _nodes = await seed_scorecard(async_db_session)
    batch = EvaluationBatch(scorecard_id=sc.id, created_by=owner.id, row_count=3, status="queued")
    async_db_session.add(batch)
    await async_db_session.flush()
    evs = await seed_queued(async_db_session, owner, version, 3)
    for e in evs:
        e.batch_id = batch.id
    await async_db_session.commit()
    d = Dispatcher(
        FakeAwsJobs(), FakeBedrockClient(converse_fn=master_converse_fn()), FakeJevScoreClient(),
        max_concurrent=3, poll_seconds=0.01, jev_retry_delays=(0.0, 0.0), patience_waits=(),
    )
    await d.run_until_idle()
    for e in evs:
        await finalize_evaluation_background(e.id)  # idempotent re-runs
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalars().all()
        assert (await db.execute(select(Evaluation).where(Evaluation.batch_id == batch.id))).scalars().all()
    assert [n.type for n in rows] == ["batch_completed"]
    assert rows[0].data["completed"] == 3 and rows[0].link == f"/evaluations?batch={batch.id}"
