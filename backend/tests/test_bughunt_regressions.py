"""Regression tests for bugs found in the security / pipeline review (one small test per fix)."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.config import get_settings
from app.models.chat_session import ChatSession
from app.models.evaluation import Evaluation
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from tests.test_auth import ANON, make_user, set_cookie_headers


def _h(user_id) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _scorecard(db: Session, owner: User, name: str = "sc") -> tuple[Scorecard, ScorecardVersion]:
    sc = Scorecard(name=name, owner_id=owner.id)
    db.add(sc)
    db.flush()
    ver = ScorecardVersion(scorecard_id=sc.id, version_number=1, created_by=owner.id)
    db.add(ver)
    db.flush()
    sc.current_version_id = ver.id
    nid = uuid.uuid4()
    db.add(KpiNode(id=nid, scorecard_version_id=ver.id, parent_id=None, path=Ltree(nid.hex), level=1,
                   name="k", weight=100, display_order=0))
    db.commit()
    return sc, ver


# --- clients cannot set pipeline-owned evaluation statuses --------------------------------------------------


def test_clients_cannot_put_an_evaluation_into_a_pipeline_status(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    _, ver = _scorecard(db_session, user)
    h = _h(user.id)
    for bad in ("queued", "ingesting", "processing", "scoring"):
        r = client.post("/api/v1/evaluations", json={"scorecard_version_id": str(ver.id), "name": "e", "status": bad},
                        headers=h)
        assert r.status_code == 422, (bad, r.text)
    ev = client.post("/api/v1/evaluations", json={"scorecard_version_id": str(ver.id), "name": "e"}, headers=h).json()
    for bad in ("queued", "ingesting", "scoring"):
        assert client.patch(f"/api/v1/evaluations/{ev['id']}", json={"status": bad}, headers=h).status_code == 422
    assert client.patch(f"/api/v1/evaluations/{ev['id']}", json={"status": "failed"}, headers=h).status_code == 200


def test_status_of_a_queued_evaluation_cannot_be_patched(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    _, ver = _scorecard(db_session, user)
    ev = Evaluation(scorecard_version_id=ver.id, name="q", evaluated_by=user.id, owner_id=user.id, status="queued")
    db_session.add(ev)
    db_session.commit()
    r = client.patch(f"/api/v1/evaluations/{ev.id}", json={"status": "completed"}, headers=_h(user.id))
    assert r.status_code == 409
    renamed = client.patch(f"/api/v1/evaluations/{ev.id}", json={"name": "renamed"}, headers=_h(user.id))
    assert renamed.status_code == 200


# --- chat reuse suggestions never expose another user's scorecards ------------------------------------------


async def test_chat_similarity_check_only_suggests_the_session_owners_scorecards(
    db_session: Session, async_db_session
) -> None:
    from app.ai.scorecard_builder import check_similarity
    from tests.fakes import FakeBedrockClient

    vec = [1.0] + [0.0] * 1023

    class _Fixed(FakeBedrockClient):
        def embed(self, text: str, *, dimensions: int = 1024) -> list[float]:
            return vec

    alice, bob = make_user(db_session, "alice@example.com"), make_user(db_session, "bob@example.com", name="Bob")
    _, bob_ver = _scorecard(db_session, bob, "Bob's secret scorecard")
    db_session.add(ScorecardEmbedding(scorecard_version_id=bob_ver.id, embedding=vec, embedding_model="t",
                                      source_text_hash="x"))
    alice_chat, bob_chat = ChatSession(user_id=alice.id), ChatSession(user_id=bob.id)
    db_session.add_all([alice_chat, bob_chat])
    db_session.commit()

    def cfg(session: ChatSession) -> dict:
        return {"configurable": {"db_session": async_db_session, "bedrock_client": _Fixed(),
                                 "thread_id": str(session.id)}}

    state = {"messages": [{"role": "user", "content": "rate things"}], "similarity_checked": False}
    assert (await check_similarity(state, cfg(alice_chat))).get("similar_suggestions") is None
    mine = await check_similarity(state, cfg(bob_chat))
    assert [s["name"] for s in mine["similar_suggestions"]] == ["Bob's secret scorecard"]
    # An unknown session fails closed.
    ghost = ChatSession(user_id=bob.id)
    ghost.id = uuid.uuid4()
    assert (await check_similarity(state, cfg(ghost))).get("similar_suggestions") is None


# --- only one turn may start on a chat session at a time ----------------------------------------------------


async def test_second_turn_cannot_claim_a_session_that_is_already_working(db_session: Session) -> None:
    from app.api.v1.chat import _mark_turn_in_progress
    from app.db import AsyncSessionLocal

    user = make_user(db_session)
    chat = ChatSession(user_id=user.id)
    db_session.add(chat)
    db_session.commit()
    async with AsyncSessionLocal() as one, AsyncSessionLocal() as two:
        await _mark_turn_in_progress(one, chat.id, require_idle=True)
        with pytest.raises(HTTPException) as exc:
            await _mark_turn_in_progress(two, chat.id, require_idle=True)
        assert exc.value.status_code == 409


def test_client_chosen_session_id_collision_is_a_conflict_not_a_500(client: TestClient, db_session: Session) -> None:
    alice, bob = make_user(db_session, "alice@example.com"), make_user(db_session, "bob@example.com", name="Bob")
    taken = ChatSession(user_id=bob.id)
    db_session.add(taken)
    db_session.commit()
    r = client.post("/api/v1/chat/sessions", json={"message": "hi", "session_id": str(taken.id)}, headers=_h(alice.id))
    assert r.status_code == 409


# --- OAuth must not hand an account pre-registered by an attacker to the real owner -------------------------


def test_oauth_link_to_an_unverified_signup_account_drops_the_squatters_password(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    import jwt

    from app.api.v1 import oauth

    s = get_settings()
    monkeypatch.setattr(s, "oauth_google_client_id", "cid")
    monkeypatch.setattr(s, "oauth_google_client_secret", "csecret")
    squatted = make_user(db_session, "victim@example.com")  # signed up (unverified) with the attacker's password
    assert squatted.email_verified_at is None and squatted.password_hash

    async def fake_profile(*_a, **_k):
        return {"subject": "g-123", "email": "victim@example.com", "email_verified": True, "name": "Victim"}

    monkeypatch.setattr(oauth, "_fetch_profile", fake_profile)
    start = client.get("/api/v1/auth/oauth/google/start", headers=ANON, follow_redirects=False)
    state_cookie = next(c for c in set_cookie_headers(start) if c.startswith("qs_oauth="))
    packed = state_cookie.split(";", 1)[0].split("=", 1)[1]
    state = jwt.decode(packed, s.jwt_secret, algorithms=[s.jwt_algorithm])["s"]
    client.cookies.set("qs_oauth", packed, path="/api/v1/auth/oauth")
    done = client.get(f"/api/v1/auth/oauth/google/callback?code=c&state={state}", headers=ANON, follow_redirects=False)
    assert done.status_code == 303 and "error" not in done.headers["location"]
    db_session.expire_all()
    user = db_session.execute(select(User).where(User.email == "victim@example.com")).scalar_one()
    assert user.password_hash is None  # the attacker's password no longer opens the account
    assert user.email_verified_at is not None
