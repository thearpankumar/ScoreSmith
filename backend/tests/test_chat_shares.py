"""Sharing a chat: read-only for the recipient (conversation + KPIs), their own saved copy, optional chart sharing,
re-sharing chains, handle (username / email-local-part) resolution, and privacy of everything else."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.v1.chat_shares as mod
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.scorecard import Scorecard
from app.models.sharing import ChatShare
from app.models.user import User
from tests.sharing_world import Team, hdr, team  # noqa: F401 - fixture

DRAFT = {
    "name": "Hackathon scorecard",
    "purpose": "Judge hackathon projects",
    "domain": "Hackathon",
    "audience": "Judges",
    "target_score": 7,
    "kpis": [
        {
            "name": "Innovation", "weight": 50, "level": 1, "parent_name": None, "included_in_scoring": True,
            "guidelines": {str(i): {"qualitative_text": f"I{i}", "quantitative_criteria": None} for i in range(11)},
        },
        {
            "name": "Execution", "weight": 50, "level": 1, "parent_name": None, "included_in_scoring": True,
            "guidelines": {str(i): {"qualitative_text": f"E{i}", "quantitative_criteria": None} for i in range(11)},
        },
    ],
}


@pytest.fixture()
def chat(db_session: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> ChatSession:
    session = ChatSession(user_id=team.owner.id, title="Build a hackathon scorecard", target_scorecard_id=team.chart["id"])
    db_session.add(session)
    db_session.flush()
    db_session.add_all(
        [
            ChatMessage(session_id=session.id, role="user", content="I need a hackathon scorecard"),
            ChatMessage(session_id=session.id, role="assistant", content="Here is a draft with 2 KPIs."),
        ]
    )
    db_session.commit()

    async def fake_state(_sid: str):
        return SimpleNamespace(draft=DRAFT, status="confirmed")

    monkeypatch.setattr(mod, "get_session_state", fake_state)
    return session


def share(client, team, chat, who, **kw):
    ident = who if isinstance(who, str) else who.email
    return client.post(f"/api/v1/chat/sessions/{chat.id}/shares", json={"identifier": ident, **kw}, headers=hdr(team.owner))


def test_a_shared_chat_is_read_only_and_shows_the_kpis(client: TestClient, team: Team, chat: ChatSession) -> None:
    assert client.get(f"/api/v1/chat/shared/{chat.id}", headers=hdr(team.invitee)).status_code == 404  # private so far
    r = share(client, team, chat, team.invitee)
    assert r.status_code == 201 and r.json()["with_chart"] is False
    note = client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"][0]
    assert note["type"] == "chat_shared" and note["link"] == f"/chat/shared/{chat.id}"
    listing = client.get("/api/v1/chat/shared", headers=hdr(team.invitee)).json()
    assert [(c["title"], c["owner_name"], c["with_chart"]) for c in listing] == [("Build a hackathon scorecard", "Olga Owner", False)]
    view = client.get(f"/api/v1/chat/shared/{chat.id}", headers=hdr(team.invitee)).json()
    assert [m["content"] for m in view["messages"]] == ["I need a hackathon scorecard", "Here is a draft with 2 KPIs."]
    assert [k["name"] for k in view["draft"]["kpis"]] == ["Innovation", "Execution"]
    assert view["can_save"] is True and view["linked_scorecard_id"] is None
    # read-only: they cannot post to it, cancel it or delete it - the owner's endpoints stay owner-only
    for method, path, body in [
        ("GET", f"/api/v1/chat/sessions/{chat.id}", None),
        ("GET", f"/api/v1/chat/sessions/{chat.id}/messages", None),
        ("POST", f"/api/v1/chat/sessions/{chat.id}/messages", {"message": "hi"}),
        ("DELETE", f"/api/v1/chat/sessions/{chat.id}", None),
    ]:
        assert client.request(method, path, json=body, headers=hdr(team.invitee)).status_code == 404, (method, path)
    # a recipient may pass the chat on, but sees only the shares THEY made (none yet), never the owner's list
    assert client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.invitee)).json() == []
    # and the chart behind it was NOT shared
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 404


def test_recipient_saves_their_own_copy_once(client: TestClient, db_session: Session, team: Team, chat: ChatSession) -> None:
    share(client, team, chat, team.invitee)
    saved = client.post(f"/api/v1/chat/shared/{chat.id}/save", headers=hdr(team.invitee))
    assert saved.status_code == 200 and saved.json()["already_saved"] is False
    sid = saved.json()["scorecard_id"]
    mine = client.get(f"/api/v1/scorecards/{sid}", headers=hdr(team.invitee)).json()
    assert mine["name"] == "Hackathon scorecard" and mine["my_role"] == "owner" and mine["is_shared"] is False
    assert client.get(f"/api/v1/scorecards/{sid}", headers=hdr(team.owner)).status_code == 404  # a private copy
    again = client.post(f"/api/v1/chat/shared/{chat.id}/save", headers=hdr(team.invitee))
    assert again.json() == {"scorecard_id": sid, "already_saved": True}
    assert db_session.scalars(select(Scorecard).where(Scorecard.owner_id == team.invitee.id)).all().__len__() == 1
    # non-recipients cannot save
    assert client.post(f"/api/v1/chat/shared/{chat.id}/save", headers=hdr(team.outsider)).status_code == 404


def test_share_chat_with_its_chart_creates_a_chart_invitation(client: TestClient, team: Team, chat: ChatSession) -> None:
    r = share(client, team, chat, "ivan", with_chart=True)  # by username
    assert r.status_code == 201, r.text
    assert r.json()["with_chart"] is True and r.json()["chart_invitation_status"] == "pending"
    inbox = [n["type"] for n in client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"]]
    assert sorted(inbox) == ["chat_shared", "invite_received"]
    inv = client.get("/api/v1/invitations", headers=hdr(team.invitee)).json()[0]
    assert client.post(f"/api/v1/invitations/{inv['id']}/accept", headers=hdr(team.invitee)).status_code == 200
    view = client.get(f"/api/v1/chat/shared/{chat.id}", headers=hdr(team.invitee)).json()
    assert view["linked_scorecard_id"] == team.sc and view["with_chart"] is True
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 200


def test_upgrade_chat_only_to_chat_and_chart_and_error_cases(
    client: TestClient, db_session: Session, team: Team, chat: ChatSession
) -> None:
    assert share(client, team, chat, team.invitee).status_code == 201
    up = share(client, team, chat, team.invitee, with_chart=True)
    assert up.status_code == 201 and up.json()["with_chart"] is True
    assert len(db_session.scalars(select(ChatShare)).all()) == 1  # one share per person
    assert share(client, team, chat, "nobody-here").json()["detail"]["code"] == "user_not_found"
    assert share(client, team, chat, team.owner).json()["detail"]["code"] == "self_invite"
    chat.target_scorecard_id = None
    db_session.commit()
    nochart = share(client, team, chat, team.outsider, with_chart=True)
    assert nochart.status_code == 422 and nochart.json()["detail"]["code"] == "no_chart_yet"
    # somebody who never received the chat can neither share, list nor revoke it
    assert client.post(f"/api/v1/chat/sessions/{chat.id}/shares", json={"identifier": "ada"}, headers=hdr(team.outsider)).status_code == 404
    assert client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.outsider)).status_code == 404


def test_revoking_a_chat_share_removes_access(client: TestClient, team: Team, chat: ChatSession) -> None:
    sh = share(client, team, chat, team.invitee).json()
    assert [s["recipient_name"] for s in client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.owner)).json()] == ["Ivan Invitee"]
    assert client.delete(f"/api/v1/chat/sessions/{chat.id}/shares/{sh['id']}", headers=hdr(team.outsider)).status_code == 404
    assert client.delete(f"/api/v1/chat/sessions/{chat.id}/shares/{sh['id']}", headers=hdr(team.owner)).status_code == 204
    assert client.get(f"/api/v1/chat/shared/{chat.id}", headers=hdr(team.invitee)).status_code == 404
    assert client.get("/api/v1/chat/shared", headers=hdr(team.invitee)).json() == []


def test_admin_has_no_bypass_for_shared_chats(client: TestClient, team: Team, chat: ChatSession) -> None:
    share(client, team, chat, team.invitee)
    assert client.get(f"/api/v1/chat/shared/{chat.id}", headers=hdr(team.admin)).status_code == 404


# --- re-sharing chains + handles --------------------------------------------------------------------------------


def test_collaborators_can_reshare_the_chart_in_a_chain(client: TestClient, team: Team) -> None:
    team.share(client)  # owner -> ivan
    r = team.invite(client, team.outsider, sender=team.invitee)  # ivan -> otto (an editor invites)
    assert r.status_code == 201, r.text
    client.post(f"/api/v1/invitations/{r.json()['id']}/accept", headers=hdr(team.outsider))
    r2 = team.invite(client, team.admin, sender=team.outsider)  # otto -> ada, three hops from the owner
    assert r2.status_code == 201
    client.post(f"/api/v1/invitations/{r2.json()['id']}/accept", headers=hdr(team.admin))
    for u in (team.owner, team.invitee, team.outsider, team.admin):
        assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(u)).status_code == 200
    sharing = client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=hdr(team.owner)).json()
    assert {c["user"]["name"] for c in sharing["collaborators"]} == {"Ivan Invitee", "Otto Outsider", "Ada Admin"}
    # an editor sees only the invitations they sent, and can revoke their own pending invite but not remove people
    mine = client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=hdr(team.invitee)).json()["invitations"]
    assert [i["invitee"]["name"] for i in mine] == ["Otto Outsider"]
    assert client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{team.admin.id}", headers=hdr(team.invitee)).status_code == 403
    # inviting the owner (or an existing collaborator) is refused
    assert team.invite(client, team.owner, sender=team.invitee).json()["detail"]["code"] == "already_collaborator"
    assert team.invite(client, team.admin, sender=team.invitee).json()["detail"]["code"] == "already_collaborator"


def test_an_editor_revokes_only_their_own_invitations(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    other = User(email="eve@example.com", name="Eve", username="eve")
    db_session.add(other)
    db_session.commit()
    by_owner = team.invite(client, other).json()
    assert client.delete(f"/api/v1/scorecards/{team.sc}/invitations/{by_owner['id']}", headers=hdr(team.invitee)).status_code == 404
    assert client.delete(f"/api/v1/scorecards/{team.sc}/invitations/{by_owner['id']}", headers=hdr(team.owner)).status_code == 200


def test_handles_resolve_by_username_then_unique_email_local_part(client: TestClient, db_session: Session, team: Team) -> None:
    legacy = User(email="legacyperson@corp.example", name="Legacy Person")  # no username (pre-0013 account)
    twin_a = User(email="sam@a.example", name="Sam A")
    twin_b = User(email="sam@b.example", name="Sam B")
    db_session.add_all([legacy, twin_a, twin_b])
    db_session.commit()
    ok = team.invite(client, "LegacyPerson")  # sign-in style handle = email local part
    assert ok.status_code == 201 and ok.json()["invitee"]["name"] == "Legacy Person"
    assert team.invite(client, "sam").json()["detail"]["code"] == "user_not_found"  # ambiguous: never resolves
    assert team.invite(client, "ivan").json()["invitee"]["name"] == "Ivan Invitee"  # explicit username wins


def test_sign_in_with_username_and_email_local_part(client: TestClient, db_session: Session) -> None:
    from app.auth import security as sec

    pw = "Correct-Horse-Battery-9"
    hashed = sec.hash_password_sync(pw)
    db_session.add_all(
        [
            User(email="named@example.com", name="Named", username="the_named_one", password_hash=hashed),
            User(email="arpankumar1119@example.com", name="Arpan", password_hash=hashed),
        ]
    )
    db_session.commit()
    anon = {"X-Test-Anonymous": "1", "Origin": "http://localhost:3000"}
    for ident in ("the_named_one", "THE_NAMED_ONE", "named@example.com", "arpankumar1119", "ArpanKumar1119@example.com"):
        r = client.post("/api/v1/auth/login", json={"email": ident, "password": pw}, headers=anon)
        assert r.status_code == 200, (ident, r.text)
    assert client.post("/api/v1/auth/login", json={"email": "nobody", "password": pw}, headers=anon).status_code == 401
    assert client.get("/api/v1/auth/config", headers=anon).json()["username_login"] is True


async def test_share_everything_cli_adds_access_without_changing_ownership(async_db_session) -> None:
    from app.models.scorecard import Scorecard as Card
    from app.scripts.share_everything import share_everything

    owner = User(email="boss@example.com", name="Boss")
    me = User(email="arpankumar1119@example.com", name="Arpan")
    async_db_session.add_all([owner, me])
    await async_db_session.flush()
    card = Card(name="Theirs", owner_id=owner.id)
    mine = Card(name="Mine", owner_id=me.id)
    async_db_session.add_all([card, mine])
    await async_db_session.flush()
    chat = ChatSession(user_id=owner.id, title="their chat", target_scorecard_id=card.id)
    async_db_session.add(chat)
    await async_db_session.commit()
    me_id, owner_id, card_id = me.id, owner.id, card.id
    dry = await share_everything("arpankumar1119", dry_run=True)
    assert dry == {"charts_added": 1, "chats_added": 1, "charts_total": 1, "chats_total": 1, "notifications_added": 0}
    assert (await async_db_session.execute(select(ChatShare))).scalars().all() == []
    done = await share_everything("arpankumar1119")
    assert done["charts_added"] == 1 and done["chats_added"] == 1
    again = await share_everything("arpankumar1119")
    assert again["charts_added"] == 0 and again["chats_added"] == 0  # idempotent
    async_db_session.expire_all()
    share = (await async_db_session.execute(select(ChatShare))).scalar_one()
    assert share.recipient_id == me_id and share.with_chart is True
    assert (await async_db_session.get(Card, card_id)).owner_id == owner_id


def test_a_chat_can_be_passed_on_in_a_chain_read_only(client: TestClient, team: Team, chat: ChatSession) -> None:
    """owner -> ivan (chat + chart) ; ivan (accepted the chart) -> otto (chat + chart) ; otto -> ada (chat only)."""
    r1 = share(client, team, chat, team.invitee, with_chart=True)
    assert r1.status_code == 201 and r1.json()["shared_by_name"] == "Olga Owner"
    inv = client.get("/api/v1/invitations", headers=hdr(team.invitee)).json()[0]
    assert client.post(f"/api/v1/invitations/{inv['id']}/accept", headers=hdr(team.invitee)).status_code == 200

    def pass_on(sender, who, **kw):
        return client.post(
            f"/api/v1/chat/sessions/{chat.id}/shares", json={"identifier": who.email, **kw}, headers=hdr(sender)
        )

    r2 = pass_on(team.invitee, team.outsider, with_chart=True)  # a recipient passes it on, with the chart invitation
    assert r2.status_code == 201 and r2.json()["shared_by_name"] == "Ivan Invitee"
    assert r2.json()["chart_invitation_status"] == "pending"
    inv2 = client.get("/api/v1/invitations", headers=hdr(team.outsider)).json()[0]
    assert client.post(f"/api/v1/invitations/{inv2['id']}/accept", headers=hdr(team.outsider)).status_code == 200
    r3 = pass_on(team.outsider, team.admin)  # third hop, chat only
    assert r3.status_code == 201

    view = client.get(f"/api/v1/chat/shared/{chat.id}", headers=hdr(team.admin)).json()
    assert view["shared_by_name"] == "Otto Outsider" and view["owner_name"] == "Olga Owner"
    assert [m["role"] for m in view["messages"]] == ["user", "assistant"] and view["linked_scorecard_id"] is None
    # still read-only and private: nobody in the chain can write into it, and the owner was told about the hops
    for who in (team.invitee, team.outsider, team.admin):
        assert client.post(f"/api/v1/chat/sessions/{chat.id}/messages", json={"message": "hi"}, headers=hdr(who)).status_code == 404
        assert client.get(f"/api/v1/chat/sessions/{chat.id}", headers=hdr(who)).status_code == 404
    owner_inbox = [n["title"] for n in client.get("/api/v1/notifications", headers=hdr(team.owner)).json()["items"]]
    assert "Ivan Invitee shared your chat with Otto Outsider" in owner_inbox
    assert "Otto Outsider shared your chat with Ada Admin" in owner_inbox
    # sees / revokes: the owner everything, a recipient only what they passed on
    assert len(client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.owner)).json()) == 3
    mine = client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.invitee)).json()
    assert [s["recipient_name"] for s in mine] == ["Otto Outsider"]
    owners_share = r1.json()["id"]
    denied = client.delete(f"/api/v1/chat/sessions/{chat.id}/shares/{r3.json()['id']}", headers=hdr(team.invitee))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "owner_only"
    assert client.delete(f"/api/v1/chat/sessions/{chat.id}/shares/{owners_share}", headers=hdr(team.outsider)).status_code == 403
    assert client.delete(f"/api/v1/chat/sessions/{chat.id}/shares/{r2.json()['id']}", headers=hdr(team.invitee)).status_code == 204
    # nobody can share it back to the owner; a duplicate is not created
    assert pass_on(team.admin, team.owner).json()["detail"]["code"] == "is_owner"
    # a recipient without access to the chart cannot attach it
    again = pass_on(team.admin, team.invitee, with_chart=True)
    assert again.status_code == 404


def test_chart_share_can_carry_the_source_chat_and_the_status_list_follows_the_invitation(
    client: TestClient, team: Team, chat: ChatSession
) -> None:
    """The chart page's Share: "Chart and its chat" shares the owner's own chat that built the chart (read-only)."""
    sharing = client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=hdr(team.owner)).json()
    assert sharing["source_chat_id"] == str(chat.id)
    # nobody else has a chat behind this chart
    assert client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=hdr(team.owner)).status_code == 200
    r = client.post(
        f"/api/v1/scorecards/{team.sc}/invitations", json={"identifier": "ivan", "include_chat": True},
        headers=hdr(team.owner),
    )
    assert r.status_code == 201 and r.json()["chat_shared"] is True
    assert sorted(n["type"] for n in client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"]) == [
        "chat_shared", "invite_received",
    ]
    listed = client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.owner)).json()
    assert [(s["recipient_name"], s["with_chart"], s["chart_invitation_status"]) for s in listed] == [
        ("Ivan Invitee", True, "pending")
    ]
    assert client.post(f"/api/v1/invitations/{r.json()['id']}/accept", headers=hdr(team.invitee)).status_code == 200
    listed = client.get(f"/api/v1/chat/sessions/{chat.id}/shares", headers=hdr(team.owner)).json()
    assert listed[0]["chart_invitation_status"] == "accepted"
    # the editing log says who joined via whom
    log = client.get(f"/api/v1/scorecards/{team.sc}/activity", headers=hdr(team.owner)).json()
    joined = [e for e in log["items"] if e["action"] == "collaborator_joined"] if isinstance(log, dict) else [
        e for e in log if e["action"] == "collaborator_joined"
    ]
    assert joined[0]["summary"] == "Ivan Invitee joined the chart (invited by Olga Owner)"
    # without a chat behind the chart the option is refused clearly; the invitation is not blocked by it
    other = client.post(
        f"/api/v1/scorecards/{team.sc}/invitations", json={"identifier": "otto", "include_chat": True},
        headers=hdr(team.invitee),
    )
    assert other.status_code == 422 and other.json()["detail"]["code"] == "no_source_chat"
    assert client.get("/api/v1/invitations", headers=hdr(team.outsider)).json() == []  # ... and nothing was sent
