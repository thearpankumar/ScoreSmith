"""Per-user concurrency: one running evaluation JOB (a batch is one job) and one running chat turn per user - never
two - while different users (also on a shared chart) are independent. Slots free on completion, failure, cancel,
lease-expiry adoption, deactivation and collaborator removal."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.chat_session import ChatSession
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from tests.sharing_world import Team, hdr, team  # noqa: F401 - fixture
from tests.test_evaluations_ai_api import DRIVE, aws  # noqa: F401 - fixture (fake AWS + dispatcher override)


def _items(n: int = 1, tag: str = "p") -> list[dict]:
    return [
        {"subject_email": f"{tag}{i}@x.com", "sources": [{"kind": "drive", "drive_url": f"{DRIVE}{i}"}]}
        for i in range(n)
    ]


def _post(client, user, sc, items, key=None):
    headers = {**hdr(user), **({"Idempotency-Key": key} if key else {})}
    return client.post("/api/v1/evaluations/ai/jobs", headers=headers, json={"scorecard_id": sc, "items": items})


def test_a_second_job_is_refused_with_a_machine_readable_code(client: TestClient, team: Team, aws) -> None:  # noqa: F811
    first = _post(client, team.owner, team.sc, _items())
    assert first.status_code == 202, first.text
    second = _post(client, team.owner, team.sc, _items())
    assert second.status_code == 409
    d = second.json()["detail"]
    assert d["code"] == "user_job_active" and "already have an evaluation running" in d["message"]
    assert d["evaluation_id"] == first.json()["evaluations"][0]["id"] and d["batch_id"] is None
    assert len(client.get("/api/v1/evaluations", headers=hdr(team.owner)).json()) == 1  # nothing was queued
    slots = client.get("/api/v1/me/slots", headers=hdr(team.owner)).json()
    assert slots["job"]["evaluation_id"] == d["evaluation_id"] and slots["chat"] is None


def test_a_batch_is_one_job_and_blocks_a_new_one(client: TestClient, team: Team, aws) -> None:  # noqa: F811
    batch = _post(client, team.owner, team.sc, _items(4))
    assert batch.status_code == 202 and batch.json()["batch_id"]
    blocked = _post(client, team.owner, team.sc, _items(1))
    assert blocked.status_code == 409
    d = blocked.json()["detail"]
    assert d["code"] == "user_job_active" and d["batch_id"] == batch.json()["batch_id"]
    assert "batch" in d["message"]
    # the batch itself was admitted whole (4 rows) although it is "one job"
    assert len(client.get("/api/v1/evaluations", headers=hdr(team.owner)).json()) == 4


def test_idempotent_replay_returns_the_original_instead_of_a_conflict(client: TestClient, team: Team, aws) -> None:  # noqa: F811
    first = _post(client, team.owner, team.sc, _items(2), key="k-1")
    replay = _post(client, team.owner, team.sc, _items(2), key="k-1")
    assert first.status_code == replay.status_code == 202
    assert replay.json()["batch_id"] == first.json()["batch_id"]
    assert _post(client, team.owner, team.sc, _items(2), key="k-2").status_code == 409  # a NEW request is refused


def test_two_simultaneous_requests_admit_exactly_one(client: TestClient, team: Team, aws) -> None:  # noqa: F811
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda i: _post(client, team.owner, team.sc, _items(1, f"c{i}")).status_code, range(6)))
    assert sorted(codes) == [202, 409, 409, 409, 409, 409], codes
    assert len(client.get("/api/v1/evaluations", headers=hdr(team.owner)).json()) == 1


def test_users_on_a_shared_chart_are_independent_and_see_each_others_rows(client: TestClient, team: Team, aws) -> None:  # noqa: F811
    team.share(client)
    mine = _post(client, team.owner, team.sc, _items(1, "o"))
    theirs = _post(client, team.invitee, team.sc, _items(3, "i"))
    assert mine.status_code == 202 and theirs.status_code == 202  # both admitted: one job each
    # each is bound by the rule individually
    assert _post(client, team.owner, team.sc, _items()).status_code == 409
    assert _post(client, team.invitee, team.sc, _items()).status_code == 409
    # rows belong to their runner but every collaborator sees all of them
    page_o = client.get("/api/v1/evaluations/page", headers=hdr(team.owner)).json()
    page_i = client.get("/api/v1/evaluations/page", headers=hdr(team.invitee)).json()
    assert page_o["total"] == page_i["total"] == 4
    runners = {(i["runner_name"], i["is_mine"]) for i in page_o["items"]}
    assert runners == {("Olga Owner", True), ("Ivan Invitee", False)}
    assert {i["runner_name"] for i in page_i["items"] if i["is_mine"]} == {"Ivan Invitee"}
    # an outsider sees none of them and is not blocked by anybody
    assert client.get("/api/v1/evaluations/page", headers=hdr(team.outsider)).json()["total"] == 0


def test_cancel_failure_and_completion_free_the_slot(client: TestClient, db_session: Session, team: Team, aws) -> None:  # noqa: F811
    first = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    assert client.post(f"/api/v1/evaluations/{first}/cancel", headers=hdr(team.owner)).status_code == 202
    second = _post(client, team.owner, team.sc, _items())
    assert second.status_code == 202  # cancelled -> free
    # a worker that died: the lease expires and another worker finishes (here: fails) the row -> free again
    eid = second.json()["evaluations"][0]["id"]
    assert _post(client, team.owner, team.sc, _items()).status_code == 409
    db_session.execute(
        Evaluation.__table__.update().where(Evaluation.id == uuid.UUID(eid)).values(
            status=EvaluationStatus.INGESTING, lease_owner="dead-worker:1",
            lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db_session.commit()
    assert _post(client, team.owner, team.sc, _items()).status_code == 409  # still active until someone resolves it
    db_session.execute(Evaluation.__table__.update().where(Evaluation.id == uuid.UUID(eid)).values(
        status=EvaluationStatus.COMPLETED))
    db_session.commit()
    assert _post(client, team.owner, team.sc, _items()).status_code == 202


def test_retry_is_subject_to_the_same_rule(client: TestClient, db_session: Session, team: Team, aws) -> None:  # noqa: F811
    first = _post(client, team.owner, team.sc, _items()).json()["evaluations"][0]["id"]
    client.post(f"/api/v1/evaluations/{first}/cancel", headers=hdr(team.owner))  # failed/cancelled
    second = _post(client, team.owner, team.sc, _items())
    assert second.status_code == 202
    retry = client.post(f"/api/v1/evaluations/{first}/retry", headers=hdr(team.owner))
    assert retry.status_code == 409 and retry.json()["detail"]["code"] == "user_job_active"
    client.post(f"/api/v1/evaluations/{second.json()['evaluations'][0]['id']}/cancel", headers=hdr(team.owner))
    assert client.post(f"/api/v1/evaluations/{first}/retry", headers=hdr(team.owner)).status_code == 202


def test_removing_a_collaborator_cancels_their_running_job_and_hides_it_from_them(
    client: TestClient, db_session: Session, team: Team, aws  # noqa: F811
) -> None:
    team.share(client)
    eid = _post(client, team.invitee, team.sc, _items()).json()["evaluations"][0]["id"]
    assert client.delete(f"/api/v1/scorecards/{team.sc}/collaborators/{team.invitee.id}",
                         headers=hdr(team.owner)).status_code == 204
    db_session.expire_all()
    ev = db_session.get(Evaluation, uuid.UUID(eid))
    assert ev.status == EvaluationStatus.FAILED and ev.error_code == "cancelled"
    # the finished row stays with the chart; the removed user can no longer see it, and their slot is free
    assert client.get(f"/api/v1/evaluations/{eid}", headers=hdr(team.invitee)).status_code == 404
    assert client.get(f"/api/v1/evaluations/{eid}", headers=hdr(team.owner)).status_code == 200
    assert client.get("/api/v1/me/slots", headers=hdr(team.invitee)).json()["job"] is None


def test_deactivating_a_user_frees_their_slots(client: TestClient, db_session: Session, team: Team, aws) -> None:  # noqa: F811
    eid = _post(client, team.outsider, _own_chart(db_session, team.outsider), _items()).json()["evaluations"][0]["id"]
    r = client.post(f"/api/v1/admin/users/{team.outsider.id}/deactivate", headers=hdr(team.admin))
    assert r.status_code == 200 and r.json()["is_active"] is False
    db_session.expire_all()
    assert db_session.get(Evaluation, uuid.UUID(eid)).status == EvaluationStatus.FAILED


def _own_chart(db: Session, user) -> str:
    from tests.sharing_world import make_chart

    return make_chart(db, user, f"{user.name}'s chart")["id"]


# --- chat slot -------------------------------------------------------------------------------------------------


def _chat(db: Session, user, *, running: bool, age_minutes: float = 0) -> ChatSession:
    s = ChatSession(user_id=user.id, title="t")
    if running:
        s.pending_turn_started_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
        s.turn_message = "hello"
    db.add(s)
    db.commit()
    return s


def test_one_chat_turn_per_user_across_sessions(client: TestClient, db_session: Session, team: Team) -> None:
    busy = _chat(db_session, team.owner, running=True)
    idle = _chat(db_session, team.owner, running=False)
    blocked = client.post(f"/api/v1/chat/sessions/{idle.id}/messages", json={"message": "hi"}, headers=hdr(team.owner))
    assert blocked.status_code == 409
    d = blocked.json()["detail"]
    assert d["code"] == "user_chat_active" and d["session_id"] == str(busy.id)
    assert client.get("/api/v1/me/slots", headers=hdr(team.owner)).json()["chat"] == {"session_id": str(busy.id)}
    # other users are unaffected
    assert client.get("/api/v1/me/slots", headers=hdr(team.invitee)).json()["chat"] is None


def test_a_stale_chat_marker_does_not_block(client: TestClient, db_session: Session, team: Team) -> None:
    _chat(db_session, team.owner, running=True, age_minutes=24 * 60)  # a crashed turn from yesterday
    assert client.get("/api/v1/me/slots", headers=hdr(team.owner)).json()["chat"] is None


def test_a_running_evaluation_and_a_running_chat_turn_coexist(client: TestClient, db_session: Session, team: Team, aws) -> None:  # noqa: F811
    _chat(db_session, team.owner, running=True)
    assert _post(client, team.owner, team.sc, _items()).status_code == 202  # chat busy, evaluation still admitted
    slots = client.get("/api/v1/me/slots", headers=hdr(team.owner)).json()
    assert slots["job"] is not None and slots["chat"] is not None
