"""A chat whose chart is in the trash (or gone) must not lead to a 404 dead end: the session reports `chart_state`
(none | active | trashed | deleted); a trashed chart's id is only handed to its owner, who can restore it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus
from app.models.scorecard import Scorecard
from tests.sharing_world import Team, hdr, team  # noqa: F401 - fixture


def _mk(db: Session, user, target: str | None, *, completed: bool = True, working: bool = False) -> ChatSession:
    s = ChatSession(
        user_id=user.id, title="Build a chart", target_scorecard_id=uuid.UUID(target) if target else None,
        status=ChatSessionStatus.COMPLETED if completed else ChatSessionStatus.ACTIVE,
        pending_turn_started_at=datetime.now(UTC) if working else None,
    )
    db.add(s)
    db.commit()
    return s


def _listed(client: TestClient, user) -> dict[str, dict]:
    return {s["id"]: s for s in client.get("/api/v1/chat/sessions", headers=hdr(user)).json()}


@pytest.fixture()
def sessions(db_session: Session, team: Team):  # noqa: F811
    return {
        "none": _mk(db_session, team.owner, None, completed=False),
        "active": _mk(db_session, team.owner, team.sc),
    }


def test_active_and_unlinked_chats(client: TestClient, team: Team, sessions) -> None:  # noqa: F811
    got = _listed(client, team.owner)
    assert got[str(sessions["none"].id)]["chart_state"] == "none"
    active = got[str(sessions["active"].id)]
    assert active["chart_state"] == "active" and active["target_scorecard_id"] == team.sc
    assert active["chart_can_restore"] is False


def test_trashed_chart_is_reported_with_a_restore_right_for_the_owner_only(
    client: TestClient, db_session: Session, team: Team, sessions  # noqa: F811
) -> None:
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).status_code == 204
    item = _listed(client, team.owner)[str(sessions["active"].id)]
    assert item["chart_state"] == "trashed" and item["chart_can_restore"] is True
    assert item["target_scorecard_id"] == team.sc  # the owner needs the id to restore it
    # restoring through the trash API turns the notice back into a normal link
    restore = client.post("/api/v1/scorecards/trash/restore", json={"ids": [team.sc]}, headers=hdr(team.owner))
    assert restore.status_code == 200
    assert _listed(client, team.owner)[str(sessions["active"].id)]["chart_state"] == "active"


def test_a_collaborators_chat_about_a_trashed_chart_gets_no_id_and_no_restore(
    client: TestClient, db_session: Session, team: Team  # noqa: F811
) -> None:
    team.share(client)
    mine = _mk(db_session, team.invitee, team.sc)  # e.g. a "refine with assistant" chat of an editor
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).status_code == 204
    item = _listed(client, team.invitee)[str(mine.id)]
    assert item["chart_state"] == "trashed" and item["chart_can_restore"] is False
    assert item["target_scorecard_id"] is None  # no id to build a dead link from


def test_a_purged_chart_is_reported_as_deleted(client: TestClient, db_session: Session, team: Team, sessions) -> None:  # noqa: F811
    db_session.delete(db_session.get(Scorecard, uuid.UUID(team.sc)))  # FK is SET NULL on the chat
    db_session.commit()
    item = _listed(client, team.owner)[str(sessions["active"].id)]
    assert item["chart_state"] == "deleted" and item["target_scorecard_id"] is None


def test_the_open_session_endpoint_carries_the_state_too(
    client: TestClient, db_session: Session, team: Team  # noqa: F811
) -> None:
    s = _mk(db_session, team.owner, team.sc, completed=True, working=True)  # no checkpoint yet -> the short branch
    assert client.get(f"/api/v1/chat/sessions/{s.id}", headers=hdr(team.owner)).json()["chart_state"] == "active"
    client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner))
    body = client.get(f"/api/v1/chat/sessions/{s.id}", headers=hdr(team.owner)).json()
    assert body["chart_state"] == "trashed" and body["chart_can_restore"] is True
    assert body["materialized_scorecard_id"] is None  # never a link to the trashed chart
