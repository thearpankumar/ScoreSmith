"""End-to-end test of the chat scorecard-builder HTTP endpoints (app/api/v1/chat.py),
through FastAPI's TestClient against the real app + real Postgres, with
`get_bedrock_client` overridden to a `FakeBedrockClient` (see tests/fakes.py — no real
Bedrock credentials in this environment)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.deps import get_bedrock_client
from app.main import app
from tests.fakes import FakeBedrockClient, tool_use_result

_COMPLETE_DRAFT_PATCH = {
    "name": "Support Ticket Quality",
    "purpose": "Rate the quality of support ticket resolutions.",
    "domain": "Customer Support",
    "audience": "Support leads",
    "target_score": 8,
    "kpis": [
        {
            "name": "Accuracy",
            "weight": 60,
            "level": 1,
            "guidelines": {"10": {"qualitative_text": "Fully accurate."}, "0": {"qualitative_text": "Wrong."}},
        },
        {
            "name": "Tone",
            "weight": 40,
            "level": 1,
            "guidelines": {"10": {"qualitative_text": "Perfectly polite."}, "0": {"qualitative_text": "Rude."}},
        },
    ],
}


@pytest.fixture(autouse=True)
def _reset_graph_singleton():
    # Each test gets its own fresh session_id (a new chat_sessions row -> new LangGraph
    # thread_id), so the module-level GraphManager singleton is safe to share; this
    # fixture just guarantees dependency_overrides never leaks between tests.
    yield
    app.dependency_overrides.pop(get_bedrock_client, None)


def test_start_session_asks_clarification_then_confirms(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-api@example.com", "name": "Chat API User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    fake1 = FakeBedrockClient(
        script=[
            tool_use_result(
                "ask_clarification",
                {"question": "What is this scorecard's purpose?", "options": [], "missing_fields": ["purpose"]},
            )
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake1

    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want to build a scorecard."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "awaiting_clarification"
    assert body["question"]["question"] == "What is this scorecard's purpose?"
    session_id = body["session_id"]

    # GET reflects the same pending question without advancing the graph.
    r = client.get(f"/api/v1/chat/sessions/{session_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_clarification"

    fake2 = FakeBedrockClient(
        script=[tool_use_result("update_draft", {"patch": _COMPLETE_DRAFT_PATCH, "confirmed": True})]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake2

    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Rate support ticket resolutions for accuracy and tone."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["materialized_scorecard_id"] is not None

    # The confirmed draft was materialized into a real, independently-readable Scorecard.
    r = client.get(f"/api/v1/scorecards/{body['materialized_scorecard_id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "Support Ticket Quality"


def test_unknown_session_returns_404(client: TestClient) -> None:
    import uuid

    r = client.get(f"/api/v1/chat/sessions/{uuid.uuid4()}")
    assert r.status_code == 404


def test_failed_first_message_leaves_no_orphaned_session(client: TestClient, seed_user_id: str) -> None:
    """Regression test for the "orphaned active session" bug: a brand-new chat's FIRST
    message used to already have a persisted, visible-as-"active" chat_sessions row (plus
    the user's chat_messages row) BEFORE the first Bedrock call, so any failure there
    (e.g. BedrockUnavailableError — this build environment has no real AWS credentials,
    so this happens for real, not just in this scripted test) left an orphaned session
    behind that the client never learned the id of and could never find again. After the
    fix (deferred persistence — see app/api/v1/chat.py::start_chat_session), a failed
    first message must leave the session list untouched, and retries (even repeated
    failing ones) must not pile up rows either."""
    from app.ai.bedrock_client import BedrockUnavailableError

    user = client.post(
        "/api/v1/users",
        json={"email": "chat-fail-first@example.com", "name": "Chat Fail First User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    def _always_fail(**_kwargs):
        raise BedrockUnavailableError("simulated Bedrock outage on first call")

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=_always_fail)

    before = client.get("/api/v1/chat/sessions", params={"user_id": user["id"]}).json()
    assert before == []

    first_message = "I want a scorecard to rate how good our internal status meetings are."

    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": first_message},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 502, r.text

    after = client.get("/api/v1/chat/sessions", params={"user_id": user["id"]}).json()
    assert after == [], "a failed first message must not leave an orphaned chat_sessions row"

    # Retrying with the same first message, still failing, must not pile up rows either.
    r2 = client.post(
        "/api/v1/chat/sessions",
        json={"message": first_message},
        headers={"X-User-Id": user["id"]},
    )
    assert r2.status_code == 502, r2.text
    still_empty = client.get("/api/v1/chat/sessions", params={"user_id": user["id"]}).json()
    assert still_empty == []

    # A subsequent retry that succeeds behaves normally and produces exactly one visible
    # session — proving the fix doesn't break the legitimate, successful path.
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[
            tool_use_result(
                "ask_clarification",
                {"question": "What should we call it?", "options": [], "missing_fields": ["name"]},
            )
        ]
    )
    r3 = client.post(
        "/api/v1/chat/sessions",
        json={"message": first_message},
        headers={"X-User-Id": user["id"]},
    )
    assert r3.status_code == 201, r3.text
    final = client.get("/api/v1/chat/sessions", params={"user_id": user["id"]}).json()
    assert len(final) == 1
    assert final[0]["id"] == r3.json()["session_id"]


class _FixedVectorFakeBedrock(FakeBedrockClient):
    """Every `.embed()` call returns the same fixed vector, so the chat graph's own
    `check_similarity` node is guaranteed to match whatever scorecard is seeded with that
    same vector directly via the DB (mirrors tests/test_suggest_similar_api.py's fake)."""

    def __init__(self, vector: list[float], script: list | None = None) -> None:
        super().__init__(script=script)
        self._vector = vector

    def embed(self, text: str, *, dimensions: int = 1024) -> list[float]:
        return self._vector


def test_start_session_surfaces_similar_scorecard_before_propose_kpis(
    client: TestClient, seed_user_id: str
) -> None:
    """Real end-to-end proof of fix #1: the LangGraph chat graph's own `check_similarity`
    node (app/ai/scorecard_builder.py) finds a near-duplicate existing scorecard on the
    session's *first* message and pauses with `status == "awaiting_similar_choice"` +
    `similar_suggestions` populated — before `propose_kpis` ever calls the (scripted, and
    therefore order-sensitive) Bedrock chat model. No `ask_clarification`/`update_draft`
    tool-use response is scripted for the first call, so this also proves propose_kpis was
    never reached on that turn (the FakeBedrockClient would raise otherwise)."""
    import asyncio
    import uuid as uuid_module

    from app.models.scorecard_embedding import EMBEDDING_DIM

    user = client.post(
        "/api/v1/users",
        json={"email": "chat-similarity@example.com", "name": "Chat Similarity User"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard = client.post(
        "/api/v1/scorecards",
        json={
            "name": "Existing Support Ticket Quality",
            "owner_id": user["id"],
            "domain": "Support",
            "purpose_statement": "Rate the quality of support ticket resolutions.",
        },
        headers={"X-User-Id": user["id"]},
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()

    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)

    async def _seed_embedding() -> None:
        from app.db import AsyncSessionLocal
        from app.models.scorecard_embedding import ScorecardEmbedding

        async with AsyncSessionLocal() as db:
            db.add(
                ScorecardEmbedding(
                    scorecard_version_id=version["id"],
                    embedding=vector,
                    embedding_model="test-synthetic",
                    source_text_hash="deadbeef",
                )
            )
            await db.commit()

    asyncio.run(_seed_embedding())

    # No .converse() script entries at all — if propose_kpis were reached before the
    # similarity pause, FakeBedrockClient.converse() would raise AssertionError.
    app.dependency_overrides[get_bedrock_client] = lambda: _FixedVectorFakeBedrock(vector, script=[])

    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I need a scorecard to rate support ticket resolutions."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "awaiting_similar_choice"
    assert body["question"] is None
    assert body["similar_suggestions"], "expected at least one suggestion"
    suggestion = body["similar_suggestions"][0]
    assert suggestion["scorecard_id"] == scorecard["id"]
    assert suggestion["name"] == "Existing Support Ticket Quality"
    assert suggestion["similarity"] > 0.99
    session_id = body["session_id"]

    # "Start fresh": resumes the same interrupt with a normal follow-up message, which
    # should now reach propose_kpis for the first time.
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[
            tool_use_result(
                "ask_clarification",
                {"question": "What should we call it?", "options": [], "missing_fields": ["name"]},
            )
        ]
    )
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Let's start fresh instead."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "awaiting_clarification"
    assert body["similar_suggestions"] is None
    assert uuid_module.UUID(body["session_id"]) == uuid_module.UUID(session_id)


def _create_scorecard_with_kpis(client: TestClient, user_id: str) -> tuple[dict, dict]:
    headers = {"X-User-Id": user_id}
    scorecard = client.post(
        "/api/v1/scorecards",
        json={
            "name": "Refinable Scorecard",
            "owner_id": user_id,
            "domain": "Support",
            "purpose_statement": "Rate support replies.",
            "scope": "Audience: Support leads",
            "target_score": 8,
        },
        headers=headers,
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user_id},
        headers=headers,
    ).json()
    guidelines = [{"score_level": lvl, "qualitative_text": f"Level {lvl}"} for lvl in range(11)]
    r = client.post(
        f"/api/v1/scorecard-versions/{version['id']}/kpi-nodes/bulk",
        json={
            "nodes": [
                {"name": "Accuracy", "level": 1, "weight": 70, "guidelines": guidelines},
                {"name": "Tone", "level": 1, "weight": 30, "guidelines": guidelines},
            ]
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    r = client.patch(
        f"/api/v1/scorecards/{scorecard['id']}", json={"current_version_id": version["id"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    return scorecard, version


def test_refine_session_seeds_draft_without_bedrock_and_saves_new_version(
    client: TestClient, seed_user_id: str
) -> None:
    """"Refine with assistant": starting a session with only `target_scorecard_id`
    pre-populates the draft from the scorecard's current version WITHOUT any model call
    (the fake has an empty script and would raise if converse() were reached), and a
    later confirm appends version 2 to the SAME scorecard instead of creating a new one."""
    scorecard, version1 = _create_scorecard_with_kpis(client, seed_user_id)
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(script=[])

    r = client.post(
        "/api/v1/chat/sessions",
        json={"target_scorecard_id": scorecard["id"]},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "gathering"
    assert body["materialized_scorecard_id"] is None
    draft = body["draft"]
    assert draft["name"] == "Refinable Scorecard"
    assert draft["audience"] == "Support leads"
    assert {k["name"]: k["weight"] for k in draft["kpis"]} == {"Accuracy": 70.0, "Tone": 30.0}
    assert all(len(k["guidelines"]) == 11 for k in draft["kpis"])
    session_id = body["session_id"]

    # GET reflects the seeded draft, and reports nothing saved yet.
    r = client.get(f"/api/v1/chat/sessions/{session_id}")
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["name"] == "Refinable Scorecard"
    assert r.json()["materialized_scorecard_id"] is None
    sessions = client.get("/api/v1/chat/sessions").json()
    assert any(s["id"] == session_id and s["target_scorecard_id"] == scorecard["id"] for s in sessions)

    # First real turn: the model renames + reweights and confirms in one call.
    fake = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {
                    "patch": {
                        "name": "Refinable Scorecard v2",
                        "kpis": [
                            {**k, "weight": 50.0} for k in draft["kpis"]
                        ],
                    },
                    "confirmed": True,
                },
            )
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Make them 50/50, rename it, and save."},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["materialized_scorecard_id"] == scorecard["id"]
    assert len(fake.calls) == 1  # exactly one propose_kpis call on this turn

    versions = client.get(f"/api/v1/scorecards/{scorecard['id']}/versions").json()
    assert [v["version_number"] for v in versions] == [1, 2]
    v2 = versions[1]
    assert body["materialized_scorecard_version_id"] == v2["id"]
    updated = client.get(f"/api/v1/scorecards/{scorecard['id']}").json()
    assert updated["current_version_id"] == v2["id"]
    assert updated["name"] == "Refinable Scorecard v2"
    v2_nodes = client.get(f"/api/v1/scorecard-versions/{v2['id']}/kpi-nodes").json()
    assert sorted(n["weight"] for n in v2_nodes) == [50.0, 50.0]
    # Version 1 is untouched.
    v1_nodes = client.get(f"/api/v1/scorecard-versions/{version1['id']}/kpi-nodes").json()
    assert sorted(n["weight"] for n in v1_nodes) == [30.0, 70.0]


def test_start_session_requires_message_without_target(client: TestClient, seed_user_id: str) -> None:
    r = client.post("/api/v1/chat/sessions", json={}, headers={"X-User-Id": seed_user_id})
    assert r.status_code == 422


def test_turn_in_progress_marker_set_during_turn_and_cleared_after(
    client: TestClient, seed_user_id: str
) -> None:
    """Part A (refresh-survives-mid-turn) regression: `chat_sessions.pending_turn_started_at`
    (surfaced as `turn_in_progress` on GET /chat/sessions/{id}) must be set for the real
    duration of a turn's graph call — proven here by querying the DB row from *inside* the
    scripted Bedrock call itself, via a completely separate DB connection (the sync engine
    — mirrors how a concurrent GET request from a refreshed browser tab would see it, on
    its own connection, while this request's own graph call is still in flight) — and must
    be cleared again once the HTTP response comes back."""
    from app.db import SyncSessionLocal
    from app.models.chat_session import ChatSession

    scorecard, _version1 = _create_scorecard_with_kpis(client, seed_user_id)

    r = client.post(
        "/api/v1/chat/sessions",
        json={"target_scorecard_id": scorecard["id"]},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    # Before any turn has run: no marker at all.
    with SyncSessionLocal() as db:
        row = db.get(ChatSession, session_id)
        assert row is not None
        assert row.pending_turn_started_at is None
        assert row.turn_in_progress is False
    r = client.get(f"/api/v1/chat/sessions/{session_id}")
    assert r.status_code == 200, r.text
    assert r.json()["turn_in_progress"] is False

    seen_in_progress: list[bool] = []

    def _assert_marked_while_running(**_kwargs):
        with SyncSessionLocal() as db:
            row = db.get(ChatSession, session_id)
            seen_in_progress.append(row.turn_in_progress if row else False)
        return tool_use_result(
            "update_draft",
            {"patch": {"name": "Refinable Scorecard v2"}, "confirmed": False},
        )

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        converse_fn=_assert_marked_while_running
    )
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Rename it."},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 200, r.text
    # The scripted patch never sets confirmed=True, so propose_kpis loops (bounded by
    # MAX_LLM_TURNS_PER_HUMAN_TURN) — the marker must still read True on every single one
    # of those Bedrock calls, proving it stays set for the graph's real full duration, not
    # just the first LLM turn within it.
    assert seen_in_progress, "expected at least one scripted Bedrock call"
    assert all(seen_in_progress), (
        "pending_turn_started_at must be set (and not yet stale) for the whole duration "
        "of the graph's Bedrock call, so a concurrent GET from a refreshed tab sees it"
    )

    # Cleared again once the request has returned.
    with SyncSessionLocal() as db:
        row = db.get(ChatSession, session_id)
        assert row is not None
        assert row.pending_turn_started_at is None
    assert r.json()["turn_in_progress"] is False


def test_turn_in_progress_marker_cleared_even_on_bedrock_failure(
    client: TestClient, seed_user_id: str
) -> None:
    """A turn that fails (BedrockUnavailableError) must still clear the marker — otherwise
    a single failed turn would wedge every future refresh of that session in "still
    working" until STALE_TURN_TIMEOUT_SECONDS finally expires it."""
    from app.ai.bedrock_client import BedrockUnavailableError
    from app.db import SyncSessionLocal
    from app.models.chat_session import ChatSession

    scorecard, _version1 = _create_scorecard_with_kpis(client, seed_user_id)
    r = client.post(
        "/api/v1/chat/sessions",
        json={"target_scorecard_id": scorecard["id"]},
        headers={"X-User-Id": seed_user_id},
    )
    session_id = r.json()["session_id"]

    def _always_fail(**_kwargs):
        raise BedrockUnavailableError("simulated outage")

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=_always_fail)
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Rename it."},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 502, r.text

    with SyncSessionLocal() as db:
        row = db.get(ChatSession, session_id)
        assert row is not None
        assert row.pending_turn_started_at is None, "marker must be cleared even when the turn fails"
