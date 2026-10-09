"""Cross-user isolation: user A holds a valid token and tries every endpoint with user B's ids.

Every one must answer 404 (never 403, never 200) so A cannot read, change, delete, run or even confirm the
existence of anything B owns. Also checks list scoping, server-set ownership, upload-key ownership and that B's
data is untouched afterwards.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.deps import get_aws_jobs
from app.main import app
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from tests.fakes import FakeAwsJobs

H_ANON = {"X-Test-Anonymous": "1"}


def _h(user_id) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


class World:
    """User A (the attacker) and user B (the victim, who owns one of everything)."""

    def __init__(self, db: Session) -> None:
        self.a = User(email="a@example.com", name="Alice")
        self.b = User(email="b@example.com", name="Bob")
        db.add_all([self.a, self.b])
        db.flush()
        self.sc = Scorecard(name="Bob's scorecard", owner_id=self.b.id, domain="Secret")
        db.add(self.sc)
        db.flush()
        self.ver = ScorecardVersion(scorecard_id=self.sc.id, version_number=1, created_by=self.b.id)
        db.add(self.ver)
        db.flush()
        self.sc.current_version_id = self.ver.id
        nid = uuid.uuid4()
        self.node = KpiNode(
            id=nid,
            scorecard_version_id=self.ver.id,
            parent_id=None,
            path=Ltree(nid.hex),
            level=1,
            name="Secret KPI",
            weight=100,
            display_order=0,
        )
        db.add(self.node)
        db.flush()
        self.guideline = KpiGuideline(kpi_node_id=self.node.id, score_level=5, qualitative_text="secret rubric")
        db.add(self.guideline)
        self.batch = EvaluationBatch(scorecard_id=self.sc.id, created_by=self.b.id, row_count=1)
        db.add(self.batch)
        db.flush()
        self.ev = Evaluation(
            scorecard_version_id=self.ver.id, name="Bob's evaluation", evaluated_by=self.b.id, batch_id=self.batch.id
        )
        db.add(self.ev)
        self.chat = ChatSession(user_id=self.b.id, title="Bob's chat")
        db.add(self.chat)
        db.flush()
        db.add(ChatMessage(session_id=self.chat.id, role="user", content="bob's secret message"))
        db.commit()
        self.b_key = f"uploads/{self.b.id}/{uuid.uuid4()}/{uuid.uuid4()}.pdf"
        self.b_batch_key = f"batches/{self.b.id}/{uuid.uuid4()}/{uuid.uuid4()}.xlsx"


@pytest.fixture()
def world(db_session: Session) -> World:
    return World(db_session)


@pytest.fixture()
def aws_fake():
    fake = FakeAwsJobs(hold=True)
    app.dependency_overrides[get_aws_jobs] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_aws_jobs, None)


def _cases(w: World) -> list[tuple[str, str, dict | None]]:
    sc, ver, node, gl, ev, batch, chat = (
        str(x) for x in (w.sc.id, w.ver.id, w.node.id, w.guideline.id, w.ev.id, w.batch.id, w.chat.id)
    )
    v = f"/api/v1/scorecards/{sc}/versions/{ver}"
    return [
        # scorecards + versions
        ("GET", f"/api/v1/scorecards/{sc}", None),
        ("PATCH", f"/api/v1/scorecards/{sc}", {"name": "hijacked"}),
        ("DELETE", f"/api/v1/scorecards/{sc}", None),
        ("GET", f"/api/v1/scorecards/{sc}/versions", None),
        ("POST", f"/api/v1/scorecards/{sc}/versions", {"version_number": 2}),
        ("GET", v, None),
        ("PATCH", v, {"guideline_notes": "x"}),
        ("DELETE", v, None),
        ("POST", f"{v}/validate-formula", {"formula": "1"}),
        ("GET", f"/api/v1/scorecard-versions/{ver}", None),
        # KPI nodes + guidelines
        ("GET", f"/api/v1/scorecard-versions/{ver}/kpi-nodes", None),
        (
            "POST",
            f"/api/v1/scorecard-versions/{ver}/kpi-nodes/bulk",
            {"nodes": [{"name": "x", "weight": 100, "level": 1, "display_order": 0}]},
        ),
        ("PATCH", "/api/v1/kpi-nodes/weights", {"weights": [{"id": node, "weight": 50}]}),
        ("GET", f"/api/v1/kpi-nodes/{node}", None),
        ("PATCH", f"/api/v1/kpi-nodes/{node}", {"name": "hijacked"}),
        ("DELETE", f"/api/v1/kpi-nodes/{node}", None),
        ("GET", f"/api/v1/kpi-nodes/{node}/guidelines", None),
        ("POST", f"/api/v1/kpi-nodes/{node}/guidelines", {"score_level": 7, "qualitative_text": "x"}),
        ("PATCH", f"/api/v1/kpi-nodes/{node}/guidelines/{gl}", {"qualitative_text": "x"}),
        ("DELETE", f"/api/v1/kpi-nodes/{node}/guidelines/{gl}", None),
        # evaluations
        ("GET", f"/api/v1/evaluations/{ev}", None),
        ("PATCH", f"/api/v1/evaluations/{ev}", {"name": "hijacked"}),
        ("DELETE", f"/api/v1/evaluations/{ev}", None),
        ("POST", f"/api/v1/evaluations/{ev}/run", {"input_text": "x"}),
        ("POST", f"/api/v1/evaluations/{ev}/finalize", None),
        ("GET", f"/api/v1/evaluations/{ev}/results", None),
        ("POST", f"/api/v1/evaluations/{ev}/results", {"kpi_node_id": node, "score": 5}),
        ("PATCH", f"/api/v1/evaluations/{ev}/results/{uuid.uuid4()}", {"score": 5}),
        ("DELETE", f"/api/v1/evaluations/{ev}/results/{uuid.uuid4()}", None),
        ("GET", f"/api/v1/evaluations/{ev}/progress", None),
        ("POST", f"/api/v1/evaluations/{ev}/cancel", None),
        ("POST", f"/api/v1/evaluations/{ev}/retry", None),
        ("GET", f"/api/v1/evaluations/ai/batches/{batch}", None),
        # creating things ON B's scorecard
        ("POST", "/api/v1/evaluations", {"scorecard_version_id": ver, "name": "x"}),
        (
            "POST",
            "/api/v1/evaluations/ai/jobs",
            {
                "scorecard_id": sc,
                "items": [{"sources": [{"kind": "drive", "drive_url": "https://drive.google.com/drive/folders/1AbC"}]}],
            },
        ),
        # chat
        ("GET", f"/api/v1/chat/sessions/{chat}", None),
        ("GET", f"/api/v1/chat/sessions/{chat}/messages", None),
        ("GET", f"/api/v1/chat/sessions/{chat}/turn-events", None),
        ("POST", f"/api/v1/chat/sessions/{chat}/messages", {"message": "hi"}),
        ("POST", f"/api/v1/chat/sessions/{chat}/cancel", None),
        ("DELETE", f"/api/v1/chat/sessions/{chat}", None),
        ("POST", "/api/v1/chat/sessions", {"message": "", "target_scorecard_id": sc}),
    ]


def test_user_a_gets_404_on_every_endpoint_with_user_b_ids(
    client: TestClient, db_session: Session, world: World, aws_fake
) -> None:
    failures = []
    for method, path, body in _cases(world):
        r = client.request(method, path, json=body, headers=_h(world.a.id))
        if r.status_code != 404:
            failures.append(f"{method} {path} -> {r.status_code} {r.text[:120]}")
    assert not failures, "\n".join(failures)

    # ...and nothing of B's changed.
    db_session.expire_all()
    assert db_session.get(Scorecard, world.sc.id).name == "Bob's scorecard"
    assert db_session.get(KpiNode, world.node.id).name == "Secret KPI"
    assert db_session.get(KpiGuideline, world.guideline.id).qualitative_text == "secret rubric"
    assert db_session.get(Evaluation, world.ev.id).name == "Bob's evaluation"
    assert db_session.get(ChatSession, world.chat.id) is not None
    assert db_session.scalars(select(Evaluation)).all() == [world.ev]  # A created nothing on B's scorecard
    assert len(db_session.scalars(select(ScorecardVersion)).all()) == 1


def test_user_b_can_use_the_same_endpoints_on_their_own_data(client: TestClient, world: World) -> None:
    """Sanity: the 404s above are about ownership, not about broken test data."""
    hb = _h(world.b.id)
    assert client.get(f"/api/v1/scorecards/{world.sc.id}", headers=hb).status_code == 200
    assert client.get(f"/api/v1/scorecard-versions/{world.ver.id}", headers=hb).status_code == 200
    assert client.get(f"/api/v1/kpi-nodes/{world.node.id}/guidelines", headers=hb).status_code == 200
    assert client.get(f"/api/v1/evaluations/{world.ev.id}", headers=hb).status_code == 200
    assert client.get(f"/api/v1/evaluations/ai/batches/{world.batch.id}", headers=hb).status_code == 200
    assert (
        client.get(f"/api/v1/chat/sessions/{world.chat.id}/messages", headers=hb).json()[0]["content"]
        == "bob's secret message"
    )


def test_lists_only_contain_the_callers_own_rows(client: TestClient, world: World) -> None:
    ha, hb = _h(world.a.id), _h(world.b.id)
    assert client.get("/api/v1/scorecards", headers=ha).json() == []
    assert client.get("/api/v1/evaluations", headers=ha).json() == []
    assert client.get("/api/v1/chat/sessions", headers=ha).json() == []
    # the old "?owner_id=" / "?user_id=" / "?evaluated_by=" filters cannot widen the scope to someone else
    assert client.get(f"/api/v1/scorecards?owner_id={world.b.id}", headers=ha).json() == []
    assert client.get(f"/api/v1/chat/sessions?user_id={world.b.id}", headers=ha).json() == []
    assert client.get(f"/api/v1/evaluations?evaluated_by={world.b.id}", headers=ha).json() == []
    assert [s["id"] for s in client.get("/api/v1/scorecards", headers=hb).json()] == [str(world.sc.id)]
    assert [e["id"] for e in client.get("/api/v1/evaluations", headers=hb).json()] == [str(world.ev.id)]
    assert [s["id"] for s in client.get("/api/v1/chat/sessions", headers=hb).json()] == [str(world.chat.id)]


def test_list_limits_are_capped_server_side(client: TestClient, world: World) -> None:
    for path in ("/api/v1/scorecards", "/api/v1/evaluations", "/api/v1/chat/sessions"):
        assert client.get(f"{path}?limit=100000", headers=_h(world.b.id)).status_code == 422
        assert client.get(f"{path}?limit=0", headers=_h(world.b.id)).status_code == 422
        assert client.get(f"{path}?skip=-1", headers=_h(world.b.id)).status_code == 422


def test_export_never_includes_another_users_evaluations(client: TestClient, world: World) -> None:
    body = {"evaluation_ids": [str(world.ev.id)]}
    r = client.post("/api/v1/evaluations/export", json=body, headers=_h(world.a.id))
    assert r.status_code == 404  # indistinguishable from "does not exist"
    # B's own (unfinished) evaluation is found but not exportable -> 422, which proves the 404 above was scoping.
    assert client.post("/api/v1/evaluations/export", json=body, headers=_h(world.b.id)).status_code == 422


def test_ownership_comes_from_the_token_never_from_the_request_body(
    client: TestClient, db_session: Session, world: World
) -> None:
    ha = _h(world.a.id)
    sc = client.post("/api/v1/scorecards", json={"name": "Mine", "owner_id": str(world.b.id)}, headers=ha)
    assert sc.status_code == 201 and sc.json()["owner_id"] == str(world.a.id)
    ver = client.post(
        f"/api/v1/scorecards/{sc.json()['id']}/versions",
        json={"version_number": 1, "created_by": str(world.b.id)},
        headers=ha,
    )
    assert ver.status_code == 201 and ver.json()["created_by"] == str(world.a.id)
    ev = client.post(
        "/api/v1/evaluations",
        json={
            "scorecard_version_id": ver.json()["id"],
            "name": "Mine",
            "evaluated_by": str(world.b.id),
            "owner_id": str(world.b.id),
        },
        headers=ha,
    )
    assert ev.status_code == 201 and ev.json()["evaluated_by"] == str(world.a.id)
    db_session.expire_all()
    assert db_session.get(Evaluation, uuid.UUID(ev.json()["id"])).owner_id == world.a.id


def test_a_scorecard_cannot_point_at_another_users_version(
    client: TestClient, db_session: Session, world: World
) -> None:
    ha = _h(world.a.id)
    mine = client.post("/api/v1/scorecards", json={"name": "Mine"}, headers=ha).json()
    r = client.patch(f"/api/v1/scorecards/{mine['id']}", json={"current_version_id": str(world.ver.id)}, headers=ha)
    assert r.status_code == 404


def test_scorecard_owner_sees_other_users_evaluations_of_their_scorecard_but_not_vice_versa(
    client: TestClient, db_session: Session, world: World
) -> None:
    """Decision: a scorecard's owner sees every evaluation of it; an evaluator sees only their own."""
    ev_by_a = Evaluation(scorecard_version_id=world.ver.id, name="Alice evaluates Bob's card", evaluated_by=world.a.id)
    db_session.add(ev_by_a)
    db_session.commit()
    seen_by_b = {e["id"] for e in client.get("/api/v1/evaluations", headers=_h(world.b.id)).json()}
    assert seen_by_b == {str(world.ev.id), str(ev_by_a.id)}
    seen_by_a = {e["id"] for e in client.get("/api/v1/evaluations", headers=_h(world.a.id)).json()}
    assert seen_by_a == {str(ev_by_a.id)}  # A's own evaluation only; Bob's evaluation stays invisible
    assert client.get(f"/api/v1/evaluations/{world.ev.id}", headers=_h(world.a.id)).status_code == 404
    # A cannot read the scorecard itself through its evaluation either.
    assert client.get(f"/api/v1/scorecards/{world.sc.id}", headers=_h(world.a.id)).status_code == 404


def test_suggest_similar_only_suggests_the_callers_own_scorecards(
    client: TestClient, db_session: Session, world: World
) -> None:
    from app.deps import get_bedrock_client
    from app.models.scorecard_embedding import ScorecardEmbedding
    from tests.fakes import FakeBedrockClient

    vec = [1.0] + [0.0] * 1023

    class _Fixed(FakeBedrockClient):
        def embed(self, text: str, *, dimensions: int = 1024) -> list[float]:
            return vec

    db_session.add(
        ScorecardEmbedding(
            scorecard_version_id=world.ver.id, embedding=vec, embedding_model="test-synthetic", source_text_hash="x"
        )
    )
    db_session.commit()
    app.dependency_overrides[get_bedrock_client] = lambda: _Fixed()
    try:
        body = {"query": "anything", "threshold": 0.5}
        assert client.post("/api/v1/scorecards/suggest-similar", json=body, headers=_h(world.a.id)).json() == []
        found = client.post("/api/v1/scorecards/suggest-similar", json=body, headers=_h(world.b.id)).json()
        assert [f["scorecard_id"] for f in found] == [str(world.sc.id)]
    finally:
        app.dependency_overrides.pop(get_bedrock_client, None)


def test_upload_keys_are_bound_to_the_user_who_got_them(client: TestClient, world: World, aws_fake) -> None:
    ha = _h(world.a.id)
    # init issues keys inside the caller's own folder, for both purposes
    sub = client.post(
        "/api/v1/evaluations/ai/uploads",
        json={"purpose": "submission", "files": [{"name": "a.pdf", "size": 10}]},
        headers=ha,
    )
    sheet = client.post(
        "/api/v1/evaluations/ai/uploads",
        json={"purpose": "batch_sheet", "files": [{"name": "s.xlsx", "size": 10}]},
        headers=ha,
    )
    assert sub.json()["files"][0]["s3_key"].startswith(f"uploads/{world.a.id}/")
    assert sheet.json()["files"][0]["s3_key"].startswith(f"batches/{world.a.id}/")

    # completing / aborting / parsing a key that belongs to B is refused (and never touches S3)
    done = client.post(
        "/api/v1/evaluations/ai/uploads/complete",
        json={"files": [{"s3_key": world.b_key, "upload_id": "u", "parts": [{"part_number": 1, "etag": "e"}]}]},
        headers=ha,
    )
    assert done.status_code == 200 and done.json()["files"][0]["ok"] is False
    assert (
        client.post("/api/v1/evaluations/ai/batches/parse", json={"s3_key": world.b_batch_key}, headers=ha).status_code
        == 422
    )
    from app.pipeline import uploads as up

    assert up.key_extension(world.b_batch_key, world.b.id, "batch_sheet") == "xlsx"  # B's own key is accepted
    assert up.key_extension(world.b_batch_key, world.a.id, "batch_sheet") is None  # ...for nobody else


def test_daily_upload_quota_is_enforced_per_user(client: TestClient, world: World, aws_fake, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "upload_user_daily_bytes", 1000)
    files = lambda n: {"purpose": "submission", "files": [{"name": "a.pdf", "size": n}]}  # noqa: E731
    assert client.post("/api/v1/evaluations/ai/uploads", json=files(900), headers=_h(world.a.id)).status_code == 200
    assert client.post("/api/v1/evaluations/ai/uploads", json=files(2000), headers=_h(world.a.id)).status_code == 429


def test_every_data_router_requires_authentication(client: TestClient, world: World) -> None:
    for method, path, body in _cases(world):
        r = client.request(method, path, json=body, headers=H_ANON)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
    for path in ("/api/v1/scorecards", "/api/v1/evaluations", "/api/v1/chat/sessions", "/api/v1/me"):
        assert client.get(path, headers=H_ANON).status_code == 401
    assert client.get("/health", headers=H_ANON).status_code == 200  # only liveness / readiness / auth are public
