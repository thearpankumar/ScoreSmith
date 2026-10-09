"""Chart trash: the owner's delete is a soft delete; a collaborator's delete is "leave"; trash list / restore / purge /
empty are strictly per user; trashed charts are invisible everywhere; expired trash is purged (idempotently)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import trash
from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import ScorecardActivity, ScorecardCollaborator
from tests.sharing_world import Team, hdr, make_chart, team  # noqa: F401 - fixture
from tests.test_eval_page import seed_rows
from tests.test_evaluations_ai_api import aws  # noqa: F401 - fixture (fake AWS + dispatcher override)
from tests.test_user_slots import _items, _post

TRASH = "/api/v1/scorecards/trash"


def _trash(client: TestClient, team: Team, who=None) -> None:
    r = client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(who or team.owner))
    assert r.status_code == 204, r.text


def _inbox_types(client: TestClient, user) -> list[str]:
    return [n["type"] for n in client.get("/api/v1/notifications", headers=hdr(user)).json()["items"]]


def _run_purge(now: datetime | None = None) -> int:
    async def go() -> int:
        async with AsyncSessionLocal() as db:
            return await trash.purge_expired(db, now)

    return asyncio.run(go())


def _backdate(db: Session, sc: str, days: float) -> None:
    card = db.get(Scorecard, uuid.UUID(sc))
    db.refresh(card)
    card.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db.commit()


# --- owner delete = soft delete -----------------------------------------------------------------------------


def test_owner_delete_hides_the_chart_everywhere_for_owner_and_collaborators(
    client: TestClient, db_session: Session, team: Team
) -> None:
    team.share(client)
    rows = seed_rows(db_session, team.owner, team.chart, 3)
    ev = str(rows[0].id)
    ev_uuid = rows[0].id
    sc, ver, node = team.sc, team.chart["version_id"], team.chart["node_ids"][0]
    _trash(client, team)
    for who in (team.owner, team.invitee):
        h = hdr(who)
        for path in (
            f"/api/v1/scorecards/{sc}",
            f"/api/v1/scorecards/{sc}/versions",
            f"/api/v1/scorecards/{sc}/versions/{ver}",
            f"/api/v1/scorecard-versions/{ver}",
            f"/api/v1/kpi-nodes/{node}/guidelines",
            f"/api/v1/scorecards/{sc}/sharing",
            f"/api/v1/scorecards/{sc}/activity",
            f"/api/v1/evaluations/{ev}",
        ):
            assert client.get(path, headers=h).status_code == 404, (who.name, path)
        assert client.get("/api/v1/scorecards", headers=h).json() == []
        assert client.get("/api/v1/evaluations", headers=h).json() == []
        page = client.get("/api/v1/evaluations/page", headers=h).json()
        assert page["items"] == []
        assert client.patch(f"/api/v1/scorecards/{sc}", json={"name": "x"}, headers=h).status_code == 404
        assert client.post(f"/api/v1/scorecards/{sc}/invitations", json={"identifier": "otto"},
                           headers=h).status_code == 404
        assert client.post(
            "/api/v1/evaluations/export", json={"evaluation_ids": [ev]}, headers=h
        ).status_code == 404
        bulk = client.post("/api/v1/evaluations/bulk-delete", json={"ids": [ev]}, headers=h)
        assert bulk.status_code == 200 and bulk.json() == {"deleted": 0, "skipped": 0}
    db_session.expire_all()
    assert db_session.get(Evaluation, ev_uuid) is not None  # nothing was deleted, only hidden


def test_trash_notifies_collaborators_and_logs_activity_and_restore_brings_everything_back(
    client: TestClient, db_session: Session, team: Team
) -> None:
    team.share(client)
    rows = seed_rows(db_session, team.owner, team.chart, 3)
    _trash(client, team)
    assert "chart_trashed" in _inbox_types(client, team.invitee)
    r = client.post(f"{TRASH}/restore", json={"ids": [team.sc]}, headers=hdr(team.owner))
    assert r.status_code == 200 and r.json() == {"done": [team.sc], "not_found": []}
    assert "chart_restored" in _inbox_types(client, team.invitee)
    # data, sharing and evaluations are all back
    for who in (team.owner, team.invitee):
        assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(who)).status_code == 200
        assert client.get(f"/api/v1/evaluations/{rows[0].id}", headers=hdr(who)).status_code == 200
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).json()["my_role"] == "editor"
    assert client.get(TRASH, headers=hdr(team.owner)).json() == []
    actions = {a.action for a in db_session.scalars(select(ScorecardActivity))}
    assert {"chart_trashed", "chart_restored"} <= actions


def test_collaborator_delete_is_leave_and_the_chart_stays_for_the_others(
    client: TestClient, db_session: Session, team: Team
) -> None:
    team.share(client)
    _trash(client, team, who=team.invitee)
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 404
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).status_code == 200
    assert client.get(TRASH, headers=hdr(team.invitee)).json() == []  # nothing lands in the editor's trash
    assert client.get(TRASH, headers=hdr(team.owner)).json() == []
    assert "collaborator_left" in _inbox_types(client, team.owner)
    db_session.expire_all()
    assert db_session.scalars(select(ScorecardCollaborator)).all() == []
    assert "collaborator_left" in {a.action for a in db_session.scalars(select(ScorecardActivity))}
    # deleting again: no access any more
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 404


def test_delete_of_a_foreign_or_trashed_chart_is_404(client: TestClient, team: Team) -> None:
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.outsider)).status_code == 404
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.admin)).status_code == 404
    _trash(client, team)
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).status_code == 404  # already gone


# --- the trash endpoints ------------------------------------------------------------------------------------


def test_listing_shows_days_left_counts_and_only_my_own_charts(
    client: TestClient, db_session: Session, team: Team
) -> None:
    team.share(client)
    seed_rows(db_session, team.owner, team.chart, 4)
    other = make_chart(db_session, team.outsider, "Outsider chart")
    _trash(client, team)
    assert client.delete(f"/api/v1/scorecards/{other['id']}", headers=hdr(team.outsider)).status_code == 204
    items = client.get(TRASH, headers=hdr(team.owner)).json()
    assert [i["id"] for i in items] == [team.sc]
    item = items[0]
    assert item["name"] == "Shared chart" and item["domain"] == "Hackathon"
    assert item["days_left"] == 30 and item["collaborator_count"] == 1 and item["evaluation_count"] == 4
    assert item["deleted_at"]
    _backdate(db_session, team.sc, 10.5)
    assert client.get(TRASH, headers=hdr(team.owner)).json()[0]["days_left"] == 20
    # the collaborator, an outsider's chart and an admin each see only their own trash
    assert client.get(TRASH, headers=hdr(team.invitee)).json() == []
    assert [i["id"] for i in client.get(TRASH, headers=hdr(team.outsider)).json()] == [other["id"]]
    assert client.get(TRASH, headers=hdr(team.admin)).json() == []


def test_nobody_else_can_restore_purge_or_empty_my_trash(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    _trash(client, team)
    for who in (team.invitee, team.outsider, team.admin):  # an admin has no bypass
        for action in ("restore", "purge"):
            r = client.post(f"{TRASH}/{action}", json={"ids": [team.sc]}, headers=hdr(who))
            assert r.status_code == 404, (who.name, action, r.text)
        assert client.post(f"{TRASH}/empty", headers=hdr(who)).json() == {"done": [], "not_found": []}
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)).deleted_at is not None  # still there
    assert client.post(f"{TRASH}/restore", json={"ids": [team.sc]}, headers=hdr(team.owner)).status_code == 200


def test_restore_and_purge_report_unknown_ids_and_validate_the_body(client: TestClient, team: Team) -> None:
    _trash(client, team)
    ghost = str(uuid.uuid4())
    r = client.post(f"{TRASH}/purge", json={"ids": [team.sc, ghost]}, headers=hdr(team.owner))
    assert r.status_code == 200 and r.json() == {"done": [team.sc], "not_found": [ghost]}
    assert client.post(f"{TRASH}/purge", json={"ids": [ghost]}, headers=hdr(team.owner)).status_code == 404
    assert client.post(f"{TRASH}/purge", json={"ids": []}, headers=hdr(team.owner)).status_code == 422
    assert client.post(f"{TRASH}/restore", json={}, headers=hdr(team.owner)).status_code == 422
    assert client.post(f"{TRASH}/restore", json={"ids": ["nope"]}, headers=hdr(team.owner)).status_code == 422


def test_purge_removes_evaluations_versions_and_sharing_for_good(
    client: TestClient, db_session: Session, team: Team
) -> None:
    team.share(client)
    ev_uuid = seed_rows(db_session, team.owner, team.chart, 3)[0].id
    keep = make_chart(db_session, team.owner, "Keep me")
    _trash(client, team)
    assert client.post(f"{TRASH}/purge", json={"ids": [team.sc]}, headers=hdr(team.owner)).status_code == 200
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)) is None
    assert db_session.get(Evaluation, ev_uuid) is None
    assert db_session.scalars(select(ScorecardCollaborator)).all() == []
    assert [v.scorecard_id for v in db_session.scalars(select(ScorecardVersion))] == [uuid.UUID(keep["id"])]
    assert client.get(TRASH, headers=hdr(team.owner)).json() == []
    assert client.post(f"{TRASH}/restore", json={"ids": [team.sc]}, headers=hdr(team.owner)).status_code == 404


def test_empty_trash_purges_everything_of_the_caller_only(client: TestClient, db_session: Session, team: Team) -> None:
    second = make_chart(db_session, team.owner, "Second")
    theirs = make_chart(db_session, team.outsider, "Theirs")
    live = make_chart(db_session, team.owner, "Live")
    _trash(client, team)
    assert client.delete(f"/api/v1/scorecards/{second['id']}", headers=hdr(team.owner)).status_code == 204
    assert client.delete(f"/api/v1/scorecards/{theirs['id']}", headers=hdr(team.outsider)).status_code == 204
    r = client.post(f"{TRASH}/empty", headers=hdr(team.owner))
    assert r.status_code == 200 and sorted(r.json()["done"]) == sorted([team.sc, second["id"]])
    assert client.get(TRASH, headers=hdr(team.owner)).json() == []
    assert len(client.get(TRASH, headers=hdr(team.outsider)).json()) == 1  # untouched
    assert [c["id"] for c in client.get("/api/v1/scorecards", headers=hdr(team.owner)).json()] == [live["id"]]
    assert client.post(f"{TRASH}/empty", headers=hdr(team.owner)).json() == {"done": [], "not_found": []}


# --- retention ---------------------------------------------------------------------------------------------


def test_purge_expired_with_an_injected_now(client: TestClient, db_session: Session, team: Team) -> None:
    _trash(client, team)
    now = datetime.now(UTC)
    assert _run_purge(now + timedelta(days=29)) == 0  # not 30 days old yet
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)) is not None
    assert _run_purge(now + timedelta(days=31)) == 1
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)) is None
    assert _run_purge(now + timedelta(days=60)) == 0  # idempotent: a second run finds nothing


def test_purge_expired_only_touches_expired_trashed_charts(client: TestClient, db_session: Session, team: Team) -> None:
    live = make_chart(db_session, team.owner, "Live")
    old, fresh = make_chart(db_session, team.owner, "Old"), make_chart(db_session, team.owner, "Fresh")
    for c in (old, fresh):
        assert client.delete(f"/api/v1/scorecards/{c['id']}", headers=hdr(team.owner)).status_code == 204
    _backdate(db_session, old["id"], 31)
    _backdate(db_session, fresh["id"], 29)
    assert _run_purge() == 1
    db_session.expire_all()
    ids = {c.id for c in db_session.scalars(select(Scorecard))}
    assert uuid.UUID(old["id"]) not in ids and {uuid.UUID(live["id"]), uuid.UUID(fresh["id"])} <= ids
    assert _run_purge() == 0


def test_listing_the_trash_lazily_purges_expired_items(client: TestClient, db_session: Session, team: Team) -> None:
    _trash(client, team)
    _backdate(db_session, team.sc, 31)
    assert client.get(TRASH, headers=hdr(team.owner)).json() == []
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)) is None


# --- invitations, jobs, admin ---------------------------------------------------------------------------


def test_an_invitation_to_a_trashed_chart_is_not_offered_or_acceptable(client: TestClient, team: Team) -> None:
    inv = team.invite(client, team.invitee)
    assert inv.status_code == 201
    iid = inv.json()["id"]
    _trash(client, team)
    assert client.get("/api/v1/invitations", headers=hdr(team.invitee)).json() == []
    for path in (f"/api/v1/invitations/{iid}/preview",):
        r = client.get(path, headers=hdr(team.invitee))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "chart_in_trash"
    r = client.post(f"/api/v1/invitations/{iid}/accept", headers=hdr(team.invitee))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "chart_in_trash"
    # after a restore the same invitation works again
    assert client.post(f"{TRASH}/restore", json={"ids": [team.sc]}, headers=hdr(team.owner)).status_code == 200
    assert client.post(f"/api/v1/invitations/{iid}/accept", headers=hdr(team.invitee)).status_code == 200


def test_jobs_cannot_start_on_a_trashed_chart_and_running_ones_are_cancelled(
    client: TestClient, db_session: Session, team: Team, aws  # noqa: F811
) -> None:
    team.share(client)
    mine = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    theirs = _post(client, team.invitee, team.sc, _items()).json()["evaluations"][0]["id"]
    _trash(client, team)  # cancels the running jobs of EVERY runner
    db_session.expire_all()
    for eid in (mine, theirs):
        ev = db_session.get(Evaluation, uuid.UUID(eid))
        assert ev.status == EvaluationStatus.FAILED and ev.error_code == "cancelled"
    for who in (team.owner, team.invitee):
        assert _post(client, who, team.sc, _items()).status_code == 404
    assert client.post(
        "/api/v1/evaluations", headers=hdr(team.owner),
        json={"scorecard_version_id": team.chart["version_id"], "name": "x", "evaluated_by": str(team.owner.id)},
    ).status_code == 404


def test_deleting_an_admin_managed_user_with_a_trashed_private_chart_purges_it(
    client: TestClient, db_session: Session, team: Team
) -> None:
    mine = make_chart(db_session, team.outsider, "Trashed private")
    assert client.delete(f"/api/v1/scorecards/{mine['id']}", headers=hdr(team.outsider)).status_code == 204
    r = client.delete(f"/api/v1/admin/users/{team.outsider.id}", headers=hdr(team.admin))
    assert r.status_code in (200, 204), r.text
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(mine["id"])) is None


def test_restoring_never_resurrects_a_cancelled_job_but_keeps_finished_evaluations(
    client: TestClient, db_session: Session, team: Team
) -> None:
    rows = seed_rows(db_session, team.owner, team.chart, 5)
    _trash(client, team)
    client.post(f"{TRASH}/restore", json={"ids": [team.sc]}, headers=hdr(team.owner))
    page = client.get("/api/v1/evaluations/page?limit=50", headers=hdr(team.owner)).json()
    assert len(page["items"]) == len(rows)
