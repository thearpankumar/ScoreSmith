"""`python -m app.scripts.share_everything`: rows are added without invitations, the recipient is notified once per
share, and re-running never duplicates rows or notifications."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.chat_session import ChatSession
from app.scripts.share_everything import share_everything
from tests.sharing_world import Team, hdr, team  # noqa: F401 - fixture


def _inbox(client: TestClient, user) -> list[dict]:
    return client.get("/api/v1/notifications", headers=hdr(user)).json()["items"]


def test_cli_shares_everything_notifies_once_and_is_idempotent(
    client: TestClient, db_session: Session, team: Team  # noqa: F811
) -> None:
    chat = ChatSession(user_id=team.owner.id, title="Owner chat", target_scorecard_id=team.chart["id"])
    db_session.add(chat)
    db_session.commit()

    dry = asyncio.run(share_everything("ivan", dry_run=True))
    assert dry["charts_added"] == 1 and dry["chats_added"] == 1 and dry["notifications_added"] == 0
    assert _inbox(client, team.invitee) == []  # a dry run changes nothing

    first = asyncio.run(share_everything("ivan"))
    assert first["charts_added"] == 1 and first["chats_added"] == 1 and first["notifications_added"] == 2
    inbox = _inbox(client, team.invitee)
    kinds = sorted(n["type"] for n in inbox)
    assert kinds == ["chart_shared", "chat_shared"]
    by_type = {n["type"]: n for n in inbox}
    assert by_type["chart_shared"]["link"] == f"/charts/{team.sc}" and "Olga Owner" in by_type["chart_shared"]["title"]
    assert by_type["chat_shared"]["link"] == f"/chat/shared/{chat.id}"

    # visible in the UI's lists, with the "shared by" labels
    charts = client.get("/api/v1/scorecards", headers=hdr(team.invitee)).json()
    assert [c["id"] for c in charts] == [team.sc] and charts[0]["my_role"] == "editor"
    assert charts[0]["owner_name"] == "Olga Owner"
    shared = client.get("/api/v1/chat/shared", headers=hdr(team.invitee)).json()
    assert [(s["session_id"], s["owner_name"]) for s in shared] == [(str(chat.id), "Olga Owner")]

    again = asyncio.run(share_everything("ivan"))
    assert again["charts_added"] == 0 and again["chats_added"] == 0 and again["notifications_added"] == 0
    assert len(_inbox(client, team.invitee)) == 2  # not duplicated
