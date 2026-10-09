"""Ownership scenarios beyond the 404 matrix: admins get no data bypass, deactivated users lose everything at once,
spoofed identity headers are inert, mixed own+foreign payloads are all-or-nothing, and ids from one of the
caller's own scorecards cannot be smuggled into another."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.auth import security as sec
from app.models.chat_turn_event import ChatTurnEvent
from app.models.evaluation import Evaluation
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from tests.test_authz_isolation import World, _cases, _h  # noqa: F401  (World/_cases reused)

H_ANON = {"X-Test-Anonymous": "1"}


def _bearer(user: User) -> dict[str, str]:
    return {**H_ANON, "Authorization": f"Bearer {sec.create_access_token(user.id)[0]}"}


def _own_card(db: Session, owner: User, name: str = "Mine"):
    """A scorecard + version + one node owned by `owner`."""
    sc = Scorecard(name=name, owner_id=owner.id)
    db.add(sc)
    db.flush()
    ver = ScorecardVersion(scorecard_id=sc.id, version_number=1, created_by=owner.id)
    db.add(ver)
    db.flush()
    sc.current_version_id = ver.id
    nid = uuid.uuid4()
    node = KpiNode(
        id=nid,
        scorecard_version_id=ver.id,
        parent_id=None,
        path=Ltree(nid.hex),
        level=1,
        name="K",
        weight=100,
        display_order=0,
    )
    db.add(node)
    db.commit()
    return sc, ver, node


def test_an_admin_has_no_data_bypass(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    admin = User(email="root@example.com", name="Root", role="admin")
    db_session.add(admin)
    db_session.commit()
    h = _bearer(admin)
    failures = []
    for method, path, body in _cases(world):
        r = client.request(method, path, json=body, headers=h)
        if r.status_code != 404:
            failures.append(f"{method} {path} -> {r.status_code}")
    assert not failures, "\n".join(failures)
    assert client.get("/api/v1/scorecards", headers=h).json() == []


def test_a_deactivated_users_token_is_dead_on_every_endpoint(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    h = _h(world.b.id)
    assert client.get(f"/api/v1/scorecards/{world.sc.id}", headers=h).status_code == 200
    world.b.is_active = False
    db_session.commit()
    codes = {client.request(m, p, json=b, headers=h).status_code for m, p, b in _cases(world)}
    assert codes == {401}


def test_identity_headers_cannot_change_who_you_are(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    spoof = {**_bearer(world.a), "X-User-Id": str(world.b.id), "X-Forwarded-User": str(world.b.id)}
    raw = TestClient(client.app)  # no test-helper translation of X-User-Id
    assert raw.get(f"/api/v1/scorecards/{world.sc.id}", headers=spoof).status_code == 404
    assert raw.get("/api/v1/me", headers=spoof).json()["id"] == str(world.a.id)


def test_a_session_cookie_of_user_a_never_sees_user_b(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    cookie_client = TestClient(client.app)
    cookie_client.cookies.set("qs_access", sec.create_access_token(world.a.id)[0])
    assert cookie_client.get("/api/v1/scorecards").json() == []
    assert cookie_client.get(f"/api/v1/chat/sessions/{world.chat.id}/messages").status_code == 404


def test_bulk_weight_update_with_one_foreign_node_changes_nothing(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    _sc, _ver, mine = _own_card(db_session, world.a)
    r = client.patch(
        "/api/v1/kpi-nodes/weights",
        json={"weights": [{"id": str(mine.id), "weight": 40}, {"id": str(world.node.id), "weight": 10}]},
        headers=_h(world.a.id),
    )
    assert r.status_code == 404
    db_session.expire_all()
    assert float(db_session.get(KpiNode, mine.id).weight) == 100.0
    assert float(db_session.get(KpiNode, world.node.id).weight) == 100.0


def test_version_id_under_the_wrong_scorecard_is_404_even_for_the_owner(
    client: TestClient, db_session: Session
) -> None:
    world = World(db_session)
    sc1, ver1, _n1 = _own_card(db_session, world.a, "One")
    sc2, _ver2, _n2 = _own_card(db_session, world.a, "Two")
    h = _h(world.a.id)
    assert client.get(f"/api/v1/scorecards/{sc1.id}/versions/{ver1.id}", headers=h).status_code == 200
    assert client.get(f"/api/v1/scorecards/{sc2.id}/versions/{ver1.id}", headers=h).status_code == 404
    assert (
        client.patch(
            f"/api/v1/scorecards/{sc2.id}/versions/{ver1.id}", json={"guideline_notes": "x"}, headers=h
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/scorecards/{sc2.id}/versions/{ver1.id}", headers=h).status_code == 404


def test_a_result_cannot_score_a_node_from_a_different_version(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    _sc1, ver1, _n1 = _own_card(db_session, world.a, "One")
    _sc2, _ver2, node2 = _own_card(db_session, world.a, "Two")
    h = _h(world.a.id)
    ev = client.post("/api/v1/evaluations", json={"scorecard_version_id": str(ver1.id), "name": "E"}, headers=h)
    assert ev.status_code == 201
    r = client.post(
        f"/api/v1/evaluations/{ev.json()['id']}/results", json={"kpi_node_id": str(node2.id), "score": 5}, headers=h
    )
    assert r.status_code == 404
    # ...and another user's node, while we are at it
    r = client.post(
        f"/api/v1/evaluations/{ev.json()['id']}/results",
        json={"kpi_node_id": str(world.node.id), "score": 5},
        headers=h,
    )
    assert r.status_code == 404


def test_guideline_of_a_sibling_node_is_not_reachable_through_another_node(
    client: TestClient, db_session: Session
) -> None:
    world = World(db_session)
    _sc, _ver, other_node = _own_card(db_session, world.b, "Bob second")
    h = _h(world.b.id)
    # world.guideline belongs to world.node, not to other_node, though B owns both
    url = f"/api/v1/kpi-nodes/{other_node.id}/guidelines/{world.guideline.id}"
    assert client.patch(url, json={"qualitative_text": "x"}, headers=h).status_code == 404
    assert client.delete(url, headers=h).status_code == 404


def test_deleting_my_data_never_touches_another_users_rows(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    sc, _ver, _n = _own_card(db_session, world.a)
    ha = _h(world.a.id)
    assert client.delete(f"/api/v1/scorecards/{sc.id}", headers=ha).status_code == 204
    chat = client.post("/api/v1/chat/sessions", json={"message": ""}, headers=ha)
    if chat.status_code in (200, 201):
        client.delete(f"/api/v1/chat/sessions/{chat.json()['session_id']}", headers=ha)
    db_session.expire_all()
    assert db_session.get(Scorecard, world.sc.id) is not None
    assert db_session.get(Evaluation, world.ev.id) is not None
    assert client.get(f"/api/v1/chat/sessions/{world.chat.id}/messages", headers=_h(world.b.id)).status_code == 200


def test_turn_events_are_per_session_and_owner_only(client: TestClient, db_session: Session) -> None:
    from datetime import UTC, datetime

    world = World(db_session)
    started = datetime.now(UTC)
    db_session.add(
        ChatTurnEvent(
            session_id=world.chat.id,
            turn_started_at=started,
            actor="orchestrator",
            event_type="status",
            message="secret step",
        )
    )
    db_session.commit()
    mine = client.get(f"/api/v1/chat/sessions/{world.chat.id}/turn-events", headers=_h(world.b.id))
    assert mine.status_code == 200 and "secret step" in mine.text
    theirs = client.get(f"/api/v1/chat/sessions/{world.chat.id}/turn-events", headers=_h(world.a.id))
    assert theirs.status_code == 404 and "secret step" not in theirs.text


def test_error_bodies_do_not_leak_whether_a_foreign_id_exists(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    h = _h(world.a.id)
    for kind, real in (("scorecards", world.sc.id), ("evaluations", world.ev.id), ("chat/sessions", world.chat.id)):
        foreign = client.get(f"/api/v1/{kind}/{real}", headers=h)
        missing = client.get(f"/api/v1/{kind}/{uuid.uuid4()}", headers=h)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
