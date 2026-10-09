"""Chart sharing: invitation lifecycle, the permission matrix (owner / editor / pending / declined / revoked /
removed / outsider / deactivated / admin), enumeration rate limit, the activity log and optimistic concurrency."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.sharing import ScorecardActivity, ScorecardCollaborator, ScorecardInvitation
from tests.sharing_world import Team, hdr, make_chart, team  # noqa: F401 - fixture

# --- sending -------------------------------------------------------------------------------------------------


def test_invite_by_email_and_by_username_notifies_the_receiver(client: TestClient, team: Team) -> None:
    by_email = team.invite(client, team.invitee)
    assert by_email.status_code == 201, by_email.text
    body = by_email.json()
    assert body["status"] == "pending" and body["invitee"]["name"] == "Ivan Invitee"
    assert body["inviter"]["name"] == "Olga Owner" and body["scorecard_name"] == "Shared chart"
    # username (any case) of a different user works the same way
    by_name = team.invite(client, "OTTO")
    assert by_name.status_code == 201 and by_name.json()["invitee"]["id"] == str(team.outsider.id)
    inbox = client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()
    assert inbox["unread"] == 1
    first = inbox["items"][0]
    assert first["type"] == "invite_received" and first["invitation_status"] == "pending"
    assert first["data"]["invitation_id"] == body["id"]


def test_unknown_inactive_and_self_targets(client: TestClient, db_session: Session, team: Team) -> None:
    unknown = team.invite(client, "nobody@example.com")
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "user_not_found"
    assert team.invite(client, "no-such-handle").json()["detail"]["code"] == "user_not_found"
    team.outsider.is_active = False
    db_session.commit()
    inactive = team.invite(client, team.outsider)
    assert inactive.status_code == 404 and inactive.json() == unknown.json()  # a deactivated user looks absent
    me = team.invite(client, team.owner)
    assert me.status_code == 422 and me.json()["detail"]["code"] == "self_invite"
    assert db_session.scalars(select(ScorecardInvitation)).all() == []


def test_duplicate_pending_and_existing_collaborator_are_handled_gracefully(client: TestClient, team: Team) -> None:
    first = team.invite(client, team.invitee)
    again = team.invite(client, team.invitee.username)
    assert again.status_code == 200 and again.json()["already_pending"] is True and again.json()["id"] == first.json()["id"]
    assert len(client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"]) == 1
    client.post(f"/api/v1/invitations/{first.json()['id']}/accept", headers=hdr(team.invitee))
    joined = team.invite(client, team.invitee)
    assert joined.status_code == 409 and joined.json()["detail"]["code"] == "already_collaborator"


def test_lookups_are_rate_limited_per_sender(client: TestClient, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_share_lookup", "3/hour")
    codes = [team.invite(client, f"ghost{i}@example.com").status_code for i in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]
    # the limit is per SENDER: another owner is unaffected
    other = make_chart(team.db, team.admin, "Admin chart")
    r = client.post(f"/api/v1/scorecards/{other['id']}/invitations", json={"identifier": "ghost@example.com"},
                    headers=hdr(team.admin))
    assert r.status_code == 404


def test_owner_and_editors_can_invite_but_strangers_cannot(client: TestClient, team: Team) -> None:
    team.share(client)
    as_editor = team.invite(client, team.outsider, sender=team.invitee)  # collaborators may re-share
    assert as_editor.status_code == 201
    as_stranger = team.invite(client, team.invitee, sender=team.outsider)
    assert as_stranger.status_code == 404


# --- receiving -----------------------------------------------------------------------------------------------


def test_preview_then_accept_gives_editor_access(client: TestClient, team: Team) -> None:
    inv = team.invite(client, team.invitee).json()
    ih = hdr(team.invitee)
    # pending: no access to the chart itself...
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=ih).status_code == 404
    assert client.get(f"/api/v1/scorecards/{team.sc}/versions", headers=ih).status_code == 404
    assert client.get(f"/api/v1/scorecard-versions/{team.chart['version_id']}", headers=ih).status_code == 404
    assert client.get("/api/v1/scorecards", headers=ih).json() == []
    # ...but a read-only preview of it
    preview = client.get(f"/api/v1/invitations/{inv['id']}/preview", headers=ih)
    assert preview.status_code == 200
    p = preview.json()
    assert p["name"] == "Shared chart" and p["owner_name"] == "Olga Owner" and len(p["kpis"]) == 2
    assert client.get(f"/api/v1/invitations/{inv['id']}/preview", headers=hdr(team.outsider)).status_code == 404
    assert [i["id"] for i in client.get("/api/v1/invitations", headers=ih).json()] == [inv["id"]]

    done = client.post(f"/api/v1/invitations/{inv['id']}/accept", headers=ih)
    assert done.status_code == 200 and done.json()["status"] == "accepted"
    mine = client.get("/api/v1/scorecards", headers=ih).json()
    assert [(c["id"], c["my_role"], c["owner_name"], c["is_shared"]) for c in mine] == [
        (team.sc, "editor", "Olga Owner", True)
    ]
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=ih).json()["collaborator_count"] == 1
    # the owner is told
    owner_inbox = client.get("/api/v1/notifications", headers=hdr(team.owner)).json()["items"]
    assert [n["type"] for n in owner_inbox] == ["invite_accepted"]
    # a second accept / a later preview is refused with a clear code
    again = client.post(f"/api/v1/invitations/{inv['id']}/accept", headers=ih)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "invitation_not_pending"
    assert client.get(f"/api/v1/invitations/{inv['id']}/preview", headers=ih).status_code == 409


def test_decline_and_revoke_leave_no_access(client: TestClient, db_session: Session, team: Team) -> None:
    declined = team.invite(client, team.invitee).json()
    r = client.post(f"/api/v1/invitations/{declined['id']}/decline", headers=hdr(team.invitee))
    assert r.status_code == 200 and r.json()["status"] == "declined"
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 404
    assert client.post(f"/api/v1/invitations/{declined['id']}/accept", headers=hdr(team.invitee)).status_code == 409
    types = [n["type"] for n in client.get("/api/v1/notifications", headers=hdr(team.owner)).json()["items"]]
    assert types == ["invite_declined"]

    # a fresh invite after a decline is allowed, and the owner can withdraw it while pending
    second = team.invite(client, team.invitee)
    assert second.status_code == 201 and second.json()["id"] != declined["id"]
    revoked = client.delete(f"/api/v1/scorecards/{team.sc}/invitations/{second.json()['id']}", headers=hdr(team.owner))
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert client.post(f"/api/v1/invitations/{second.json()['id']}/accept", headers=hdr(team.invitee)).status_code == 409
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 404
    again = client.delete(f"/api/v1/scorecards/{team.sc}/invitations/{second.json()['id']}", headers=hdr(team.owner))
    assert again.status_code == 409
    sharing = client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=hdr(team.owner)).json()
    assert [i["status"] for i in sharing["invitations"]] == ["revoked", "declined"]  # newest first
    assert sharing["collaborators"] == []


def test_invitations_belong_to_their_invitee_only(client: TestClient, team: Team) -> None:
    inv = team.invite(client, team.invitee).json()
    for who in (team.outsider, team.owner, team.admin):
        assert client.post(f"/api/v1/invitations/{inv['id']}/accept", headers=hdr(who)).status_code == 404
        assert client.post(f"/api/v1/invitations/{inv['id']}/decline", headers=hdr(who)).status_code == 404
    assert client.delete(f"/api/v1/scorecards/{team.sc}/invitations/{inv['id']}", headers=hdr(team.outsider)).status_code == 404


# --- the permission matrix -----------------------------------------------------------------------------------


def _editor_matrix(team: Team):
    sc, ver, node = team.sc, team.chart["version_id"], team.chart["node_ids"][0]
    v = f"/api/v1/scorecards/{sc}/versions/{ver}"
    return [
        ("GET", f"/api/v1/scorecards/{sc}", None),
        ("PATCH", f"/api/v1/scorecards/{sc}", {"name": "Renamed"}),
        ("GET", f"/api/v1/scorecards/{sc}/versions", None),
        ("GET", v, None),
        ("PATCH", v, {"guideline_notes": "n"}),
        ("GET", f"/api/v1/scorecard-versions/{ver}", None),
        ("GET", f"/api/v1/scorecard-versions/{ver}/kpi-nodes", None),
        ("GET", f"/api/v1/kpi-nodes/{node}", None),
        ("PATCH", f"/api/v1/kpi-nodes/{node}", {"name": "KPI A"}),
        ("GET", f"/api/v1/kpi-nodes/{node}/guidelines", None),
        ("POST", f"/api/v1/scorecards/{sc}/versions", {"version_number": 2}),
        ("GET", f"/api/v1/scorecards/{sc}/activity", None),
        ("GET", f"/api/v1/scorecards/{sc}/sharing", None),
    ]


def test_editor_can_do_what_the_owner_can_except_owner_only_actions(client: TestClient, team: Team) -> None:
    team.share(client)
    eh = hdr(team.invitee)
    for method, path, body in _editor_matrix(team):
        r = client.request(method, path, json=body, headers=eh)
        assert r.status_code in (200, 201), f"{method} {path} -> {r.status_code} {r.text[:150]}"
    sharing = client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=eh).json()
    assert sharing["my_role"] == "editor" and sharing["invitations"] == []  # editors see only invitations THEY sent
    r = client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{team.invitee.id}", headers=eh)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "owner_only"  # removing people stays owner-only
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).json()["name"] == "Renamed"
    # "Delete" by an editor only removes THEIR access (the same as leaving): the owner keeps the chart.
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=eh).status_code == 204
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=eh).status_code == 404
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).status_code == 200


@pytest.mark.parametrize("state", ["outsider", "pending", "declined", "revoked", "removed", "left", "admin"])
def test_everyone_else_gets_404_on_the_whole_chart(client: TestClient, team: Team, state: str) -> None:
    who = team.outsider
    if state == "admin":
        who = team.admin  # no data bypass
    elif state != "outsider":
        who = team.invitee
        if state == "pending":
            team.invite(client, who)
        elif state == "declined":
            client.post(f"/api/v1/invitations/{team.invite(client, who).json()['id']}/decline", headers=hdr(who))
        elif state == "revoked":
            inv = team.invite(client, who).json()
            client.delete(f"/api/v1/scorecards/{team.sc}/invitations/{inv['id']}", headers=hdr(team.owner))
        elif state == "removed":
            team.share(client, who)
            assert client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{who.id}",
                                 headers=hdr(team.owner)).status_code == 204
        elif state == "left":
            team.share(client, who)
            assert client.post(f"/api/v1/scorecards/{team.sc}/leave", headers=hdr(who)).status_code == 204
    failures = []
    for method, path, body in _editor_matrix(team) + [("DELETE", f"/api/v1/scorecards/{team.sc}", None)]:
        r = client.request(method, path, json=body, headers=hdr(who))
        if r.status_code != 404:
            failures.append(f"{method} {path} -> {r.status_code}")
    assert not failures, failures
    assert client.get("/api/v1/scorecards", headers=hdr(who)).json() == []


def test_a_deactivated_collaborator_is_locked_out_everywhere(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    team.invitee.is_active = False
    db_session.commit()
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 401


def test_admin_collaborates_in_both_directions_without_a_bypass(client: TestClient, team: Team) -> None:
    # user -> admin
    team.share(client, team.admin)
    assert client.patch(f"/api/v1/kpi-nodes/{team.chart['node_ids'][0]}", json={"name": "By admin"},
                        headers=hdr(team.admin)).status_code == 200
    # admin -> user
    admin_chart = make_chart(team.db, team.admin, "Admin's chart")
    assert client.get(f"/api/v1/scorecards/{admin_chart['id']}", headers=hdr(team.owner)).status_code == 404
    inv = client.post(f"/api/v1/scorecards/{admin_chart['id']}/invitations", json={"identifier": team.owner.username},
                      headers=hdr(team.admin))
    assert inv.status_code == 201
    client.post(f"/api/v1/invitations/{inv.json()['id']}/accept", headers=hdr(team.owner))
    assert client.get(f"/api/v1/scorecards/{admin_chart['id']}", headers=hdr(team.owner)).status_code == 200


def test_removing_a_collaborator_and_leaving(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    gone = client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{team.invitee.id}", headers=hdr(team.owner))
    assert gone.status_code == 204
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).status_code == 404
    told = client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"]
    assert told[0]["type"] == "collaborator_removed"
    assert db_session.scalars(select(ScorecardCollaborator)).all() == []
    assert client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{team.invitee.id}",
                         headers=hdr(team.owner)).status_code == 404
    # re-invite works and the owner cannot "leave"
    team.share(client)
    assert client.post(f"/api/v1/scorecards/{team.sc}/leave", headers=hdr(team.owner)).status_code == 422
    assert client.post(f"/api/v1/scorecards/{team.sc}/leave", headers=hdr(team.invitee)).status_code == 204
    assert client.post(f"/api/v1/scorecards/{team.sc}/leave", headers=hdr(team.invitee)).status_code == 404


def test_deleting_the_chart_removes_sharing_rows(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    assert client.delete(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).status_code == 204
    # The owner's delete only moves the chart to the trash (sharing is kept so a restore brings it back) ...
    assert client.get("/api/v1/scorecards", headers=hdr(team.invitee)).json() == []
    assert len(db_session.scalars(select(ScorecardCollaborator)).all()) == 1
    # ... and the permanent delete (purge) removes the sharing rows with the chart.
    r = client.post("/api/v1/scorecards/trash/purge", json={"ids": [team.sc]}, headers=hdr(team.owner))
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.scalars(select(ScorecardCollaborator)).all() == []
    assert db_session.scalars(select(ScorecardInvitation)).all() == []
    assert db_session.scalars(select(ScorecardActivity)).all() == []
    assert client.get("/api/v1/scorecards", headers=hdr(team.invitee)).json() == []


# --- activity log --------------------------------------------------------------------------------------------


def test_activity_log_records_who_changed_what(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    node0, node1 = team.chart["node_ids"]
    eh, oh = hdr(team.invitee), hdr(team.owner)
    assert client.patch(f"/api/v1/scorecards/{team.sc}", json={"target_score": 8}, headers=eh).status_code == 200
    assert client.patch("/api/v1/kpi-nodes/weights", headers=oh,
                        json={"weights": [{"id": node0, "weight": 60}, {"id": node1, "weight": 40}]}).status_code == 200
    assert client.patch(f"/api/v1/kpi-nodes/{node0}", json={"name": "Renamed KPI"}, headers=eh).status_code == 200
    gl = client.get(f"/api/v1/kpi-nodes/{node0}/guidelines", headers=oh).json()[0]
    assert client.patch(f"/api/v1/kpi-nodes/{node0}/guidelines/{gl['id']}", json={"qualitative_text": "new"},
                        headers=eh).status_code == 200
    assert client.post(f"/api/v1/scorecards/{team.sc}/versions", json={"version_number": 2}, headers=oh).status_code == 201
    bulk = client.post(
        f"/api/v1/scorecard-versions/{team.chart['version_id']}/kpi-nodes/bulk", headers=eh,
        json={"nodes": [{"name": "Extra", "weight": 0, "level": 1, "display_order": 5}]},
    )
    assert bulk.status_code == 201, bulk.text
    assert client.delete(f"/api/v1/kpi-nodes/{bulk.json()[0]['id']}", headers=oh).status_code == 204
    log = client.get(f"/api/v1/scorecards/{team.sc}/activity", headers=oh).json()
    by_action = {(e["action"], e["actor_name"]) for e in log["items"]}
    assert {
        ("collaborator_invited", "Olga Owner"), ("collaborator_joined", "Ivan Invitee"),
        ("scorecard_updated", "Ivan Invitee"), ("weights_changed", "Olga Owner"), ("kpi_updated", "Ivan Invitee"),
        ("guideline_updated", "Ivan Invitee"), ("version_created", "Olga Owner"), ("kpi_added", "Ivan Invitee"),
        ("kpi_deleted", "Olga Owner"),
    } <= by_action
    assert all(e["created_at"] and e["summary"] for e in log["items"])
    ids = [e["id"] for e in log["items"]]
    assert ids == sorted(ids, reverse=True)  # newest first


def test_activity_log_is_paginated_and_append_only(client: TestClient, db_session: Session, team: Team) -> None:
    team.share(client)
    for i in range(7):
        client.patch(f"/api/v1/scorecards/{team.sc}", json={"purpose_statement": f"v{i}"}, headers=hdr(team.owner))
    page1 = client.get(f"/api/v1/scorecards/{team.sc}/activity?limit=4", headers=hdr(team.invitee)).json()
    assert len(page1["items"]) == 4 and page1["next_before"] == page1["items"][-1]["id"]
    page2 = client.get(f"/api/v1/scorecards/{team.sc}/activity?limit=100&before={page1['next_before']}",
                       headers=hdr(team.invitee)).json()
    assert page2["next_before"] is None
    seen = [e["id"] for e in page1["items"] + page2["items"]]
    assert len(seen) == len(set(seen)) == 2 + 7  # invited + joined + 7 edits
    # the table refuses UPDATEs
    with pytest.raises(Exception, match="append-only"):
        db_session.execute(text("UPDATE scorecard_activity SET summary = 'tampered'"))
    db_session.rollback()


def test_activity_is_invisible_to_non_collaborators(client: TestClient, team: Team) -> None:
    assert client.get(f"/api/v1/scorecards/{team.sc}/activity", headers=hdr(team.outsider)).status_code == 404
    assert client.get(f"/api/v1/scorecards/{team.sc}/sharing", headers=hdr(team.outsider)).status_code == 404


def test_edits_notify_the_other_collaborators_once_per_window(client: TestClient, team: Team) -> None:
    team.share(client)
    for i in range(3):
        client.patch(f"/api/v1/scorecards/{team.sc}", json={"purpose_statement": f"x{i}"}, headers=hdr(team.invitee))
    owner_types = [n["type"] for n in client.get("/api/v1/notifications", headers=hdr(team.owner)).json()["items"]]
    assert owner_types.count("chart_edited") == 1  # coalesced
    own_types = [n["type"] for n in client.get("/api/v1/notifications", headers=hdr(team.invitee)).json()["items"]]
    assert "chart_edited" not in own_types  # never notified of one's own edit


# --- optimistic concurrency ----------------------------------------------------------------------------------


def test_stale_edit_is_rejected_with_409_and_current_state(client: TestClient, team: Team) -> None:
    team.share(client)
    seen = client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).json()["updated_at"]
    # the owner saves first
    assert client.patch(f"/api/v1/scorecards/{team.sc}", json={"name": "Owner's title"}, headers=hdr(team.owner)).status_code == 200
    lost = client.patch(f"/api/v1/scorecards/{team.sc}", json={"name": "Editor's title"},
                        headers={**hdr(team.invitee), "If-Match": seen})
    assert lost.status_code == 409
    d = lost.json()["detail"]
    assert d["code"] == "stale_edit" and "changed by someone else" in d["message"] and d["updated_at"] != seen
    assert client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.owner)).json()["name"] == "Owner's title"
    # with the fresh stamp (or without the header) the edit goes through
    fresh = client.get(f"/api/v1/scorecards/{team.sc}", headers=hdr(team.invitee)).json()["updated_at"]
    ok = client.patch(f"/api/v1/scorecards/{team.sc}", json={"name": "Merged"}, headers={**hdr(team.invitee), "If-Match": fresh})
    assert ok.status_code == 200 and ok.json()["name"] == "Merged"
    assert client.patch(f"/api/v1/scorecards/{team.sc}", json={"name": "Merged 2"}, headers=hdr(team.owner)).status_code == 200


def test_stale_edit_on_kpi_nodes_and_guidelines(client: TestClient, team: Team) -> None:
    team.share(client)
    node = team.chart["node_ids"][0]
    gl = client.get(f"/api/v1/kpi-nodes/{node}/guidelines", headers=hdr(team.owner)).json()[0]
    node_seen = client.get(f"/api/v1/kpi-nodes/{node}", headers=hdr(team.invitee)).json()["updated_at"]
    client.patch(f"/api/v1/kpi-nodes/{node}", json={"name": "Owner rename"}, headers=hdr(team.owner))
    assert client.patch(f"/api/v1/kpi-nodes/{node}", json={"name": "Mine"},
                        headers={**hdr(team.invitee), "If-Match": node_seen}).status_code == 409
    client.patch(f"/api/v1/kpi-nodes/{node}/guidelines/{gl['id']}", json={"qualitative_text": "a"}, headers=hdr(team.owner))
    assert client.patch(f"/api/v1/kpi-nodes/{node}/guidelines/{gl['id']}", json={"qualitative_text": "b"},
                        headers={**hdr(team.invitee), "If-Match": gl["updated_at"]}).status_code == 409
