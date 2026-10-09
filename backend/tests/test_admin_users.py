"""Admin user management and the two-role model: server-side enforcement (403 for users), last-admin and self guards,
session revocation, username sign-in and deletion = anonymisation that keeps shared charts working."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.chat_session import ChatSession
from app.models.evaluation import Evaluation
from app.models.notification import Notification
from app.models.scorecard import Scorecard
from app.models.sharing import ScorecardActivity, ScorecardCollaborator
from app.models.user import User
from tests.sharing_world import Team, bearer, hdr, make_chart, team  # noqa: F401 - fixture

ADMIN = "/api/v1/admin/users"
GOOD_PW = "Correct-Horse-Battery-9"
ADMIN_PW = "Admin-Correct-Horse-Battery-9!"


def create(client: TestClient, admin: User, **kw):
    body = {"email": "new@example.com", "name": "New Person", "password": GOOD_PW, **kw}
    return client.post(ADMIN, json=body, headers=hdr(admin))


def login(client: TestClient, ident: str, password: str):
    return TestClient(client.app).post("/api/v1/auth/login", json={"email": ident, "password": password},
                                       headers={"Origin": "http://localhost:3000"})


# --- authorization ---------------------------------------------------------------------------------------------


def test_every_admin_endpoint_is_403_for_a_normal_user_and_401_anonymous(client: TestClient, team: Team) -> None:
    uid = str(team.invitee.id)
    calls = [
        ("GET", ADMIN, None), ("POST", ADMIN, {"email": "x@example.com", "name": "x", "password": GOOD_PW}),
        ("PATCH", f"{ADMIN}/{uid}", {"name": "y"}), ("POST", f"{ADMIN}/{uid}/password", {"password": GOOD_PW}),
        ("POST", f"{ADMIN}/{uid}/deactivate", None), ("POST", f"{ADMIN}/{uid}/reactivate", None),
        ("DELETE", f"{ADMIN}/{uid}", None),
    ]
    for method, path, body in calls:
        r = client.request(method, path, json=body, headers=hdr(team.owner))
        assert r.status_code == 403, (method, path, r.status_code)
        r = client.request(method, path, json=body, headers={"X-Test-Anonymous": "1"})
        assert r.status_code == 401, (method, path, r.status_code)
    db_user = client.get("/api/v1/auth/me", headers=hdr(team.owner)).json()
    assert db_user["role"] == "user"


def test_create_list_search_and_filter(client: TestClient, team: Team) -> None:
    made = create(client, team.admin, username="Neo.Anderson", role="user")
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["username"] == "neo.anderson" and body["role"] == "user" and body["is_active"] and "password" not in body
    listing = client.get(ADMIN, headers=hdr(team.admin)).json()
    assert listing["total"] == 5 and {u["role"] for u in listing["items"]} == {"admin", "user"}
    assert [u["email"] for u in client.get(f"{ADMIN}?q=anderson", headers=hdr(team.admin)).json()["items"]] == ["new@example.com"]
    assert client.get(f"{ADMIN}?role=admin", headers=hdr(team.admin)).json()["total"] == 1
    assert client.get(f"{ADMIN}?q=%25", headers=hdr(team.admin)).json()["total"] == 0
    team.invitee.is_active = False
    team.db.commit()
    assert client.get(f"{ADMIN}?active=false", headers=hdr(team.admin)).json()["total"] == 1


def test_create_validates_role_uniqueness_and_password(client: TestClient, team: Team) -> None:
    assert create(client, team.admin, role="superuser").status_code == 422
    assert create(client, team.admin, role="member").status_code == 422  # the legacy roles are gone
    assert create(client, team.admin, password="short").status_code == 422
    assert create(client, team.admin, role="admin", password="Correct-Hors1").status_code == 422  # admin policy: 14+
    assert create(client, team.admin, role="admin", password=ADMIN_PW).status_code == 201
    dupe = create(client, team.admin, email="NEW@example.com")
    assert dupe.status_code == 409 and dupe.json()["detail"]["code"] == "email_taken"
    assert create(client, team.admin, email="o@example.com", username="OLGA").json()["detail"]["code"] == "username_taken"
    assert create(client, team.admin, email="p@example.com", username="not valid!").status_code == 422


def test_patch_role_name_email_and_username(client: TestClient, db_session: Session, team: Team) -> None:
    uid = str(team.invitee.id)
    r = client.patch(f"{ADMIN}/{uid}", json={"name": "Ivan II", "email": "ivan2@example.com", "role": "admin",
                                              "username": "ivan2"}, headers=hdr(team.admin))
    assert r.status_code == 200 and (r.json()["role"], r.json()["username"]) == ("admin", "ivan2")
    assert client.patch(f"{ADMIN}/{uid}", json={"clear_username": True}, headers=hdr(team.admin)).json()["username"] is None
    assert client.patch(f"{ADMIN}/{uid}", json={"role": "root"}, headers=hdr(team.admin)).status_code == 422
    assert client.patch(f"{ADMIN}/{uid}", json={"email": team.owner.email}, headers=hdr(team.admin)).status_code == 409
    assert client.patch(f"{ADMIN}/{uuid.uuid4()}", json={"name": "x"}, headers=hdr(team.admin)).status_code == 404
    log = db_session.scalars(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(uid))).all()
    assert any(entry.diff and entry.diff.get("event") == "admin_user_updated" for entry in log)


# --- guards ----------------------------------------------------------------------------------------------------


def test_self_guards(client: TestClient, team: Team) -> None:
    me = str(team.admin.id)
    for method, path, body in [
        ("POST", f"{ADMIN}/{me}/deactivate", None), ("DELETE", f"{ADMIN}/{me}", None),
        ("PATCH", f"{ADMIN}/{me}", {"role": "user"}),
    ]:
        r = client.request(method, path, json=body, headers=hdr(team.admin))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "self_action", (method, r.text)
    assert client.patch(f"{ADMIN}/{me}", json={"name": "Ada Lovelace"}, headers=hdr(team.admin)).status_code == 200


def test_two_admins_cannot_remove_each_other_leaving_none(client: TestClient, db_session: Session, team: Team) -> None:
    other = User(email="root2@example.com", name="Root Two", role="admin")
    db_session.add(other)
    db_session.commit()
    # With two active admins one may demote the other...
    assert client.patch(f"{ADMIN}/{other.id}", json={"role": "user"}, headers=hdr(team.admin)).status_code == 200
    # ...but the remaining one is now the last active admin and nobody (not even a stale second session) can remove it.
    r = client.post(f"{ADMIN}/{team.admin.id}/deactivate", headers=hdr(team.admin))
    assert r.json()["detail"]["code"] == "self_action"


async def test_guard_refuses_to_remove_the_last_active_admin(async_db_session) -> None:
    from app.user_admin import AdminRuleError, guard_admin_change

    sole = User(email="sole@example.com", name="Sole", role="admin")
    ghost = User(email="ghost@example.com", name="Ghost", role="user")  # an actor who is not an active admin
    async_db_session.add_all([sole, ghost])
    await async_db_session.commit()
    sole_id, ghost_id = sole.id, ghost.id
    for what in ("deactivate", "delete", "change the role of"):
        actor, target = await async_db_session.get(User, ghost_id), await async_db_session.get(User, sole_id)
        with pytest.raises(AdminRuleError) as exc:
            await guard_admin_change(async_db_session, actor, target, losing_admin=True, what=what)
        assert exc.value.code == "last_admin"
        await async_db_session.rollback()
    backup = User(email="backup@example.com", name="Backup", role="admin")
    async_db_session.add(backup)
    await async_db_session.commit()
    actor, target = await async_db_session.get(User, ghost_id), await async_db_session.get(User, sole_id)
    await guard_admin_change(async_db_session, actor, target, losing_admin=True, what="delete")  # a second admin exists


def test_concurrent_mutual_demotion_leaves_an_admin(client: TestClient, db_session: Session, team: Team) -> None:
    from concurrent.futures import ThreadPoolExecutor

    other = User(email="root2@example.com", name="Root Two", role="admin")
    db_session.add(other)
    db_session.commit()
    jobs = [(team.admin, other), (other, team.admin)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(
            lambda j: client.patch(f"{ADMIN}/{j[1].id}", json={"role": "user"}, headers=hdr(j[0])).status_code, jobs))
    assert sorted(codes) in ([200, 409], [200, 401]), codes
    db_session.expire_all()
    roles = [u.role for u in db_session.scalars(select(User)).all() if u.role == "admin"]
    assert len(roles) >= 1  # never zero active admins


# --- sessions and sign-in --------------------------------------------------------------------------------------


def test_new_user_can_sign_in_by_email_or_username_and_deactivation_is_immediate(client: TestClient, team: Team) -> None:
    create(client, team.admin, username="newbie")
    by_email = login(client, "new@example.com", GOOD_PW)
    by_name = login(client, "NewBie", GOOD_PW)
    assert by_email.status_code == 200 and by_name.status_code == 200, (by_email.text, by_name.text)
    token = by_name.json()["access_token"]
    auth = {"X-Test-Anonymous": "1", "Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/scorecards", headers=auth).status_code == 200
    uid = by_name.json()["user"]["id"]
    assert client.post(f"{ADMIN}/{uid}/deactivate", headers=hdr(team.admin)).status_code == 200
    assert client.get("/api/v1/scorecards", headers=auth).status_code == 401  # the live token dies at once
    assert login(client, "newbie", GOOD_PW).status_code == 401
    assert client.post(f"{ADMIN}/{uid}/reactivate", headers=hdr(team.admin)).status_code == 200
    assert login(client, "newbie", GOOD_PW).status_code == 200
    assert client.get("/api/v1/scorecards", headers=auth).status_code == 401  # but the old token stays revoked


def test_password_reset_revokes_sessions_and_old_password(client: TestClient, team: Team) -> None:
    create(client, team.admin)
    old = login(client, "new@example.com", GOOD_PW)
    auth = {"X-Test-Anonymous": "1", "Authorization": f"Bearer {old.json()['access_token']}"}
    uid = old.json()["user"]["id"]
    assert client.post(f"{ADMIN}/{uid}/password", json={"password": "weak"}, headers=hdr(team.admin)).status_code == 422
    fresh = "Another-Strong-Passphrase-42"
    assert client.post(f"{ADMIN}/{uid}/password", json={"password": fresh}, headers=hdr(team.admin)).status_code == 200
    assert client.get("/api/v1/scorecards", headers=auth).status_code == 401
    assert login(client, "new@example.com", GOOD_PW).status_code == 401
    assert login(client, "new@example.com", fresh).status_code == 200


def test_role_change_revokes_sessions(client: TestClient, team: Team) -> None:
    create(client, team.admin)
    tok = login(client, "new@example.com", GOOD_PW).json()
    auth = {"X-Test-Anonymous": "1", "Authorization": f"Bearer {tok['access_token']}"}
    assert client.get(ADMIN, headers=auth).status_code == 403
    client.patch(f"{ADMIN}/{tok['user']['id']}", json={"role": "admin"}, headers=hdr(team.admin))
    assert client.get(ADMIN, headers=auth).status_code == 401
    again = login(client, "new@example.com", GOOD_PW).json()["access_token"]
    assert client.get(ADMIN, headers={"X-Test-Anonymous": "1", "Authorization": f"Bearer {again}"}).status_code == 200


def test_a_users_own_role_cannot_be_escalated_through_me(client: TestClient, team: Team) -> None:
    r = client.patch("/api/v1/me", json={"name": "Hacker", "role": "admin"}, headers=hdr(team.owner))
    assert r.status_code == 200 and r.json()["role"] == "user"


# --- deletion = anonymisation ----------------------------------------------------------------------------------


def test_deleting_a_user_keeps_shared_charts_and_removes_private_data(
    client: TestClient, db_session: Session, team: Team
) -> None:
    team.share(client)  # owner's chart is shared with the invitee
    private = make_chart(db_session, team.owner, "Olga private")
    db_session.add(ChatSession(user_id=team.owner.id, title="private chat"))
    db_session.add(Notification(user_id=team.owner.id, type="x", title="t"))
    db_session.commit()
    client.patch(f"/api/v1/scorecards/{team.sc}", json={"purpose_statement": "p"}, headers=hdr(team.owner))
    # a run by the owner on the shared chart stays with the chart
    ev = Evaluation(scorecard_version_id=uuid.UUID(team.chart["version_id"]), name="kept", evaluated_by=team.owner.id,
                    owner_id=team.owner.id, status="completed")
    db_session.add(ev)
    db_session.commit()
    r = client.delete(f"{ADMIN}/{team.owner.id}", headers=hdr(team.admin))
    assert r.status_code == 204
    db_session.expire_all()
    gone = db_session.get(User, team.owner.id)
    assert gone.deleted_at is not None and gone.is_active is False and gone.name == "Deleted user"
    assert gone.email.endswith("@deleted.invalid") and gone.password_hash is None and gone.username is None
    # shared chart survives, now owned by the collaborator; they are no longer "collaborator" but owner
    card = db_session.get(Scorecard, uuid.UUID(team.sc))
    assert card is not None and card.owner_id == team.invitee.id
    assert db_session.scalars(select(ScorecardCollaborator)).all() == []
    assert client.patch(f"/api/v1/scorecards/{team.sc}", json={"name": "Mine now"}, headers=hdr(team.invitee)).status_code == 200
    assert client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{uuid.uuid4()}", headers=hdr(team.invitee)).status_code == 404
    # private chart, chats, notifications are gone; the evaluation they ran is labelled with the tombstone
    assert db_session.get(Scorecard, uuid.UUID(private["id"])) is None
    assert db_session.scalars(select(ChatSession).where(ChatSession.user_id == team.owner.id)).all() == []
    assert db_session.scalars(select(Notification).where(Notification.user_id == team.owner.id)).all() == []
    page = client.get("/api/v1/evaluations/page", headers=hdr(team.invitee)).json()
    assert [(i["name"], i["runner_name"]) for i in page["items"]] == [("kept", "Deleted user")]
    # the editing log keeps its lines but names nobody
    log = db_session.scalars(select(ScorecardActivity).where(ScorecardActivity.scorecard_id == uuid.UUID(team.sc))).all()
    assert log and "Olga Owner" not in {e.actor_name for e in log} and "Deleted user" in {e.actor_name for e in log}
    # no login, and the deleted account vanishes from the admin list and from invitations
    assert login(client, team.owner.email, "anything").status_code == 401
    assert all(u["id"] != str(team.owner.id) for u in client.get(ADMIN, headers=hdr(team.admin)).json()["items"])
    assert client.delete(f"{ADMIN}/{team.owner.id}", headers=hdr(team.admin)).status_code == 404
    other = make_chart(db_session, team.outsider, "Otto's")
    ghost = client.post(f"/api/v1/scorecards/{other['id']}/invitations", json={"identifier": "olga"}, headers=hdr(team.outsider))
    assert ghost.status_code == 404


def test_deleting_an_unshared_users_charts_cascades_cleanly(client: TestClient, db_session: Session, team: Team) -> None:
    db_session.add(Evaluation(scorecard_version_id=uuid.UUID(team.chart["version_id"]), name="e", evaluated_by=team.owner.id,
                              owner_id=team.owner.id, status="completed"))
    db_session.commit()
    assert client.delete(f"{ADMIN}/{team.owner.id}", headers=hdr(team.admin)).status_code == 204
    db_session.expire_all()
    assert db_session.get(Scorecard, uuid.UUID(team.sc)) is None
    assert db_session.scalars(select(Evaluation)).all() == []


def test_admin_role_is_no_bypass_for_charts_or_evaluations(client: TestClient, team: Team) -> None:
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=bearer(team.admin)).status_code == 404
    assert client.get("/api/v1/scorecards", headers=bearer(team.admin)).json() == []


def test_users_change_their_own_password(client: TestClient, team: Team) -> None:
    create(client, team.admin)
    tok = login(client, "new@example.com", GOOD_PW).json()["access_token"]
    auth = {"X-Test-Anonymous": "1", "Authorization": f"Bearer {tok}"}
    url = "/api/v1/me/password"
    assert client.post(url, json={"current_password": "wrong", "new_password": "Another-Strong-Passphrase-42"},
                       headers=auth).status_code == 403
    assert client.post(url, json={"current_password": GOOD_PW, "new_password": "weak"}, headers=auth).status_code == 422
    ok = client.post(url, json={"current_password": GOOD_PW, "new_password": "Another-Strong-Passphrase-42"}, headers=auth)
    assert ok.status_code == 200
    assert client.get("/api/v1/scorecards", headers=auth).status_code == 401  # signed out everywhere
    assert login(client, "new@example.com", GOOD_PW).status_code == 401
    assert login(client, "new@example.com", "Another-Strong-Passphrase-42").status_code == 200
