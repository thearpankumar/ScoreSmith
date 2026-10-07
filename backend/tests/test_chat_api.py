"""End-to-end test of the chat scorecard-builder HTTP endpoints (app/api/v1/chat.py),
through FastAPI's TestClient against the real app + real Postgres, with
`get_bedrock_client` overridden to a `FakeBedrockClient` (see tests/fakes.py — no real
Bedrock credentials in this environment)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.deps import get_bedrock_client, get_jev_client, get_web_search_client
from app.main import app
from tests.fakes import FakeBedrockClient, FakeJevClient, full_rubric, text_result, tool_use_result

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
            "guidelines": full_rubric(),
        },
        {
            "name": "Tone",
            "weight": 40,
            "level": 1,
            "guidelines": full_rubric(),
        },
    ],
}


@pytest.fixture(autouse=True)
def _reset_graph_singleton():
    # Each test gets its own fresh session_id (a new chat_sessions row -> new LangGraph
    # thread_id), so the module-level GraphManager singleton is safe to share.
    #
    # Default `get_web_search_client`/`get_jev_client` to inert fakes for every test in
    # this file (regression fix: these two were never overridden here before, so whether
    # a script-based test's fixed-length `FakeBedrockClient` script was actually
    # sufficient silently depended on whatever real client `app/deps.py` resolves those
    # two dependencies to in the environment the suite happens to run in — None/
    # unconfigured in CI, but REAL, live-API-key-backed clients in this project's dev
    # container per infra/.env, which made `research_kpis`'s own angle-decision Bedrock
    # call and/or a real Jev quality-gate score-triggered retry fire silently extra,
    # unscripted `.converse()` calls that exhausted the script — a real, confirmed-live
    # flakiness gap, not a hypothetical one). `FakeWebSearchClient`/`FakeJevClient`'s
    # defaults (empty results / always-pass) make every test here hermetic regardless of
    # which real external services happen to be configured wherever it runs; an
    # individual test can still override either explicitly if it wants different
    # behavior (e.g. an in-progress research fan-out).
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()
    yield
    app.dependency_overrides.pop(get_bedrock_client, None)
    app.dependency_overrides.pop(get_web_search_client, None)
    app.dependency_overrides.pop(get_jev_client, None)


def test_start_session_asks_clarification_then_confirms(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-api@example.com", "name": "Chat API User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    fake1 = FakeBedrockClient(
        script=[
            # Issue 1: the very first Bedrock call on a brand-new session's first message
            # is the fast/cheap title-generation call (see
            # app/api/v1/chat.py::_generate_and_persist_title) — BEFORE the graph's own
            # first call. Order matters here (FakeBedrockClient pops its script in order).
            text_result("Support Ticket Quality Review"),
            tool_use_result(
                "ask_clarification",
                {"question": "What is this scorecard's purpose?", "options": [], "missing_fields": ["purpose"]},
            ),
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
    # Real, AI-generated title — not a truncated raw message — already present on the
    # very first response, before any follow-up turn.
    assert body["title"] == "Support Ticket Quality Review"
    session_id = body["session_id"]

    # GET reflects the same pending question without advancing the graph, and still
    # carries the same title (generated once, never regenerated).
    r = client.get(f"/api/v1/chat/sessions/{session_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_clarification"
    assert r.json()["title"] == "Support Ticket Quality Review"

    # The session list (what the sidebar polls) reflects it too, with no page reload
    # needed — this is the same real title, visible from a completely separate request.
    sessions = client.get("/api/v1/chat/sessions", params={"user_id": user["id"]}).json()
    assert any(s["id"] == session_id and s["title"] == "Support Ticket Quality Review" for s in sessions)

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
            text_result("Status Meeting Quality"),  # title-generation call (Issue 1) — see above
            tool_use_result(
                "ask_clarification",
                {"question": "What should we call it?", "options": [], "missing_fields": ["name"]},
            ),
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
        from app.config import get_settings
        from app.db import AsyncSessionLocal
        from app.models.scorecard_embedding import ScorecardEmbedding

        async with AsyncSessionLocal() as db:
            db.add(
                ScorecardEmbedding(
                    scorecard_version_id=version["id"],
                    embedding=vector,
                    embedding_model=get_settings().embedding_model_id,
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
    # Refine-session titles are deterministic (the scorecard name gives a perfectly good
    # title for free) — no Bedrock call needed, which is also why FakeBedrockClient(script=[])
    # above is safe here (nothing ever calls .converse() on this path until the real turn below).
    assert body["title"] == 'Refining "Refinable Scorecard"'
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


def test_update_scoring_formula_reachable_from_chat_and_materializes(
    client: TestClient, seed_user_id: str
) -> None:
    """Issue 2 (see task notes): `update_scoring_formula` is a bound chat tool on every
    turn (see `_TOOLS` in app/ai/scorecard_builder.py, unconditional — not gated behind
    "already materialized" the way the chart-detail-only UI previously made it feel), and
    setting it through chat updates `draft.scoring_formula` in live LangGraph state
    exactly like `update_draft` patches KPIs — then carries through to the real,
    materialized `ScorecardVersion.scoring_formula` on confirm."""
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-formula@example.com", "name": "Chat Formula User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    formula = 'min(kpi["Accuracy"], kpi["Tone"])'

    fake1 = FakeBedrockClient(
        script=[
            text_result("Support Formula Scorecard"),  # title-generation call (Issue 1)
            # A non-confirming update_draft always loops back to propose_kpis for another
            # LLM turn within the same human turn (see scorecard_builder.py's graph
            # edges) — script a second response (ask_clarification, to pause) to close it.
            tool_use_result("update_draft", {"patch": _COMPLETE_DRAFT_PATCH, "confirmed": False}),
            tool_use_result(
                "ask_clarification",
                {"question": "Anything else before I save it?", "options": [], "missing_fields": []},
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake1
    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want to build a scorecard rating accuracy and tone."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]
    assert r.json()["draft"]["scoring_formula"] is None

    fake2 = FakeBedrockClient(
        script=[
            tool_use_result("update_scoring_formula", {"formula": formula}),
            # update_scoring_formula never completes the draft by itself and always loops
            # back to propose_kpis for another LLM turn within the same human turn (see
            # scorecard_builder.py's graph edges) — script a second response to close it.
            tool_use_result(
                "ask_clarification",
                {"question": "Anything else to change?", "options": [], "missing_fields": []},
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake2
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Use the minimum of Accuracy and Tone instead of averaging them."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["scoring_formula"] == formula

    # GET reflects the same live LangGraph state (no turn advanced).
    r = client.get(f"/api/v1/chat/sessions/{session_id}")
    assert r.json()["draft"]["scoring_formula"] == formula

    fake3 = FakeBedrockClient(script=[tool_use_result("update_draft", {"patch": {}, "confirmed": True})])
    app.dependency_overrides[get_bedrock_client] = lambda: fake3
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Looks good, save it."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    version_id = body["materialized_scorecard_version_id"]
    assert version_id is not None

    version = client.get(
        f"/api/v1/scorecards/{body['materialized_scorecard_id']}/versions/{version_id}"
    ).json()
    assert version["scoring_formula"] == formula


def test_update_draft_assistant_message_is_shown_verbatim_and_patch_is_applied(
    client: TestClient, seed_user_id: str
) -> None:
    """Regression test for a real bug found live: the assistant would describe a drafted
    scorecard/KPIs in rich prose via `respond_conversationally` INSTEAD of actually calling
    `update_draft`, leaving the persisted draft's structured fields (name/purpose/kpis/...)
    empty despite the chat transcript sounding like real work had happened — see
    UPDATE_DRAFT_TOOL's `assistant_message` field and propose_kpis's `assistant_note`
    derivation in app/ai/scorecard_builder.py.

    The fix closes the structural incentive for that: `update_draft`'s hardcoded
    "Updating the draft." chat-bubble text (the only message previously shown) gave the
    model nowhere to put a genuine, descriptive explanation EXCEPT `respond_conversationally`
    (whose reply never touches the draft). Now `update_draft` itself carries a real
    user-facing `assistant_message`, so describing what was drafted and actually drafting
    it happen in the very same tool call. This test proves both halves together: the
    message shown to the user is the model's own real text (not the old generic label),
    AND the patch it describes is genuinely reflected in the persisted draft — not just a
    nice-sounding chat bubble with nothing behind it."""
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-assistant-message@example.com", "name": "Chat Assistant Message User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    descriptive_message = (
        "I've drafted a comprehensive support-quality scorecard with 2 KPIs covering "
        "Accuracy and Tone. Let me know if you'd like to adjust the weights."
    )
    fake = FakeBedrockClient(
        script=[
            text_result("Support Ticket Quality Review"),  # title-generation call
            # confirmed=True + a complete patch so this is the turn's ONLY LLM call (LLM
            # first, human second — mirrors test_llm_first_propose_then_confirm_in_one_turn
            # in tests/test_scorecard_builder.py). A non-confirming update_draft always
            # loops back to propose_kpis for another LLM turn (see scorecard_builder.py's
            # graph edges), which would make a second node's own message the final visible
            # one instead of this one — not what this test is checking.
            tool_use_result(
                "update_draft",
                {
                    "patch": _COMPLETE_DRAFT_PATCH,
                    "confirmed": True,
                    "assistant_message": descriptive_message,
                },
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    # Two sources of UNSCRIPTED extra Bedrock calls this environment can trigger (both
    # absent in CI, where neither external service is configured, but both real and
    # reachable in this dev container via infra/.env's live API keys):
    #  - research_kpis's own angle-decision call, if web_search_client is usable — None
    #    makes it a clean, documented no-op (see its own docstring).
    #  - A real Jev quality-gate score below threshold triggering propose_kpis's own
    #    retry loop (see MAX_QUALITY_GATE_RETRIES) — FakeJevClient's default (no script)
    #    always scores 1.0, so the gate always passes on the first attempt, matching what
    #    this test's fixed-length script assumes.
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()

    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want a scorecard rating support ticket accuracy and tone."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "confirmed"

    # The real, descriptive message the model wrote is what's shown — not the old
    # hardcoded generic placeholder update_draft's node used to always produce
    # ("Draft updated and confirmed complete.") regardless of what the model actually said.
    assert body["assistant_message"] == descriptive_message

    # And the content it describes was genuinely persisted, not just narrated: the draft
    # this session now holds actually has the name/KPIs the message claims were drafted.
    assert body["draft"]["name"] == _COMPLETE_DRAFT_PATCH["name"]
    assert [k["name"] for k in body["draft"]["kpis"]] == [k["name"] for k in _COMPLETE_DRAFT_PATCH["kpis"]]

    # GET reflects the same persisted state (this is real chat_messages content, not just
    # the in-memory graph response) — the transcript itself carries the real message too.
    messages = client.get(f"/api/v1/chat/sessions/{body['session_id']}/messages").json()
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert assistant_messages[-1]["content"] == descriptive_message


def test_update_draft_without_assistant_message_falls_back_to_generic_text(
    client: TestClient, seed_user_id: str
) -> None:
    """Safety net for a model that ignores the (required-in-schema-only, not
    server-enforced) `assistant_message` field: `update_draft`'s node falls back to its
    original generic note (unchanged from before this field existed) rather than showing
    an empty bubble."""
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-assistant-message-fallback@example.com", "name": "Fallback User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    fake = FakeBedrockClient(
        script=[
            text_result("Fallback Scorecard"),
            # confirmed=True (unlike the other tests here) so this resolves in a single
            # propose_kpis visit — a non-confirming update_draft always loops back for
            # another LLM turn (see scorecard_builder.py's graph edges), which isn't
            # relevant to what this test checks and would need a second scripted response.
            tool_use_result("update_draft", {"patch": _COMPLETE_DRAFT_PATCH, "confirmed": True}),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    app.dependency_overrides[get_web_search_client] = lambda: None  # see comment above, same reason
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()  # see comment above, same reason

    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want a scorecard rating support ticket accuracy and tone."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["assistant_message"] == "Draft updated and confirmed complete."


def test_update_scoring_formula_reachable_via_chat_applies_patch(
    client: TestClient, seed_user_id: str
) -> None:
    """Mechanical (patch-application) half of the `update_scoring_formula`
    `assistant_message` regression coverage — see
    test_update_draft_assistant_message_is_shown_verbatim_and_patch_is_applied for the
    `update_draft` half, and
    tests/test_scorecard_builder.py::test_update_scoring_formula_prefers_assistant_message_over_generic_note
    for the message-preference half, tested directly at the node level.

    `update_scoring_formula` unconditionally loops back to `propose_kpis` for another LLM
    turn within the same human turn (see scorecard_builder.py's graph edges — it "never
    completes the draft by itself"), so whatever tool call comes right after it (here,
    `ask_clarification`) is always what ends up as THIS turn's final, HTTP-visible
    `assistant_message` — not `update_scoring_formula`'s own. That's why the node-level
    unit test above is what actually proves the message-preference fix for this tool; this
    test instead proves the real, end-to-end thing IT is responsible for: the formula
    genuinely reaches the persisted draft via the real chat/HTTP path, not just in
    isolation."""
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-formula-message@example.com", "name": "Chat Formula Message User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    fake1 = FakeBedrockClient(
        script=[
            text_result("Formula Message Scorecard"),
            tool_use_result("update_draft", {"patch": _COMPLETE_DRAFT_PATCH, "confirmed": False}),
            tool_use_result(
                "ask_clarification",
                {"question": "Anything else before I save it?", "options": [], "missing_fields": []},
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake1
    app.dependency_overrides[get_web_search_client] = lambda: None  # see comment above, same reason
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()  # see comment above, same reason
    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want a scorecard rating accuracy and tone."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    formula_message = "Switching to the minimum of Accuracy and Tone instead of a plain average."
    fake2 = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_scoring_formula",
                {"formula": 'min(kpi["Accuracy"], kpi["Tone"])', "assistant_message": formula_message},
            ),
            tool_use_result(
                "ask_clarification",
                {"question": "Anything else to change?", "options": [], "missing_fields": []},
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake2
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Use the minimum of Accuracy and Tone instead of averaging them."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    # The turn's final visible message is the ask_clarification that followed (see
    # docstring above) — update_scoring_formula's own assistant_message is proven at the
    # node level instead.
    assert "Anything else to change?" not in r.json()["assistant_message"]  # the card carries the question
    assert r.json()["draft"]["scoring_formula"] == 'min(kpi["Accuracy"], kpi["Tone"])'


def test_session_deleted_mid_turn_returns_409_not_500(client: TestClient, seed_user_id: str) -> None:
    """Regression test for a real race found live: `start_session`'s graph call can run
    for minutes (the research fan-out), long enough for the session to be deleted out from
    under it by a concurrent `DELETE /chat/sessions/{id}` (e.g. another tab, or another
    client altogether) before the turn finishes. Every write after the graph call has a
    `chat_sessions.id` foreign key, so this used to surface as an unhandled
    `IntegrityError` — a raw 500 with a SQL traceback leaking to the client — confirmed
    live via the exact scenario this test reproduces mechanically: the scripted Bedrock
    response deletes the session (via a separate DB connection/transaction, exactly like a
    real concurrent request would) before returning its decision, so persistence
    afterward genuinely hits the now-missing foreign key. See the try/except
    `IntegrityError` in app/api/v1/chat.py::start_chat_session."""
    from app.db import SyncSessionLocal
    from app.models.chat_session import ChatSession as ChatSessionModel

    user = client.post(
        "/api/v1/users",
        json={"email": "chat-race@example.com", "name": "Chat Race User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    session_id = "11111111-2222-4333-8444-555555555555"

    def _delete_session_then_decide(**_kwargs):
        with SyncSessionLocal() as db:
            db.query(ChatSessionModel).filter(ChatSessionModel.id == session_id).delete()
            db.commit()
        return tool_use_result(
            "ask_clarification",
            {"question": "What's the purpose?", "options": [], "missing_fields": ["purpose"]},
        )

    # The title-generation call happens first and must succeed normally (it's what
    # actually creates+commits the session row this test then deletes); only the graph's
    # own decision call triggers the delete.
    calls_seen = {"n": 0}

    def _converse_fn(**kwargs):
        calls_seen["n"] += 1
        if calls_seen["n"] == 1:
            return text_result("Race Condition Test Session")
        return _delete_session_then_decide(**kwargs)

    fake = FakeBedrockClient(converse_fn=_converse_fn)
    app.dependency_overrides[get_bedrock_client] = lambda: fake

    r = client.post(
        "/api/v1/chat/sessions",
        json={"session_id": session_id, "message": "I want a scorecard."},
        headers={"X-User-Id": user["id"]},
    )

    assert r.status_code == 409, r.text
    assert "deleted" in r.json()["detail"].lower()

    # No orphaned chat_messages row was left behind for the now-nonexistent session either
    # (the whole persistence block — including the user's own message — is inside the same
    # try/except and rolled back together).
    messages = client.get(f"/api/v1/chat/sessions/{session_id}/messages")
    assert messages.status_code == 404


def test_validate_formula_draft_endpoint_checks_against_given_kpi_names(client: TestClient) -> None:
    """The generic, scorecard-less validator (Issue 2) that the chat live-preview panel's
    formula editor calls — same underlying `app/ai/scoring_formula.py::validate` the
    per-version endpoint uses, just given KPI names directly instead of looking them up
    from a materialized version (the chat draft has none yet)."""
    r = client.post(
        "/api/v1/scorecards/validate-formula",
        json={"formula": 'min(kpi["Accuracy"], kpi["Tone"])', "kpi_names": ["Accuracy", "Tone"]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"valid": True, "error": None, "unused_kpis": []}

    r = client.post(
        "/api/v1/scorecards/validate-formula",
        json={"formula": 'kpi["Nonexistent"]', "kpi_names": ["Accuracy", "Tone"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False
    assert body["error"]

    r = client.post("/api/v1/scorecards/validate-formula", json={"formula": None, "kpi_names": []})
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True


# --- DELETE /chat/sessions/{id} (hover-delete in the SessionList sidebar) ---------------


def test_delete_chat_session_requires_auth(client: TestClient, seed_user_id: str) -> None:
    """Matches the existing pattern (`DELETE /scorecards/{id}` / `DELETE /evaluations/{id}`
    — see app/api/v1/scorecards.py / evaluations.py): just the dev-auth-stub dependency,
    no `X-User-Id` -> 401, same as every other mutating route in this app."""
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-delete-auth@example.com", "name": "Chat Delete Auth User"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[
            text_result("Delete Me"),
            tool_use_result(
                "ask_clarification",
                {"question": "What's the purpose?", "options": [], "missing_fields": ["purpose"]},
            ),
        ]
    )
    r = client.post(
        "/api/v1/chat/sessions", json={"message": "delete me"}, headers={"X-User-Id": user["id"]}
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    r = client.delete(f"/api/v1/chat/sessions/{session_id}")
    assert r.status_code == 401


def test_delete_chat_session_unknown_returns_404(client: TestClient, seed_user_id: str) -> None:
    import uuid as uuid_mod

    r = client.delete(f"/api/v1/chat/sessions/{uuid_mod.uuid4()}", headers={"X-User-Id": seed_user_id})
    assert r.status_code == 404


def test_delete_chat_session_removes_row_messages_and_checkpoints(
    client: TestClient, seed_user_id: str, db_session
) -> None:
    """Real end-to-end proof (per the task's "verify for real" instruction, and matching
    how a prior pass had to manually clean up orphaned test sessions via raw SQL against
    these exact tables — see app/ai/scorecard_builder.py::delete_session_checkpoints):
    deleting a chat session removes (a) the `chat_sessions` row itself, (b) its
    `chat_messages`/`chat_turn_events` rows via their `ON DELETE CASCADE` FKs, and (c)
    every LangGraph checkpoint row for its `thread_id` in the `checkpoints`/
    `checkpoint_writes` tables — a completely separate schema LangGraph owns, which no
    plain relational cascade would ever touch."""
    from sqlalchemy import text as sa_text

    user = client.post(
        "/api/v1/users",
        json={"email": "chat-delete@example.com", "name": "Chat Delete User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[
            text_result("Delete Me"),
            tool_use_result(
                "ask_clarification",
                {"question": "What's the purpose?", "options": [], "missing_fields": ["purpose"]},
            ),
        ]
    )
    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want to build a scorecard to delete."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    # Real LangGraph checkpoint state exists for this thread_id (the same GET the
    # frontend uses to load a resumed session).
    assert client.get(f"/api/v1/chat/sessions/{session_id}").status_code == 200
    checkpoints_before = db_session.execute(
        sa_text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": session_id}
    ).scalar_one()
    assert checkpoints_before > 0

    messages_before = client.get(f"/api/v1/chat/sessions/{session_id}/messages").json()
    assert len(messages_before) > 0

    r = client.delete(f"/api/v1/chat/sessions/{session_id}", headers={"X-User-Id": user["id"]})
    assert r.status_code == 204, r.text

    # The relational row — and its messages/turn-events via ON DELETE CASCADE — are gone.
    assert client.get(f"/api/v1/chat/sessions/{session_id}/messages").status_code == 404
    sessions = client.get("/api/v1/chat/sessions", params={"user_id": user["id"]}).json()
    assert sessions == []

    # LangGraph's own checkpoint rows for this thread_id are gone too — not just the
    # relational chat_sessions/chat_messages rows.
    checkpoints_after = db_session.execute(
        sa_text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"), {"tid": session_id}
    ).scalar_one()
    assert checkpoints_after == 0
    writes_after = db_session.execute(
        sa_text("SELECT count(*) FROM checkpoint_writes WHERE thread_id = :tid"), {"tid": session_id}
    ).scalar_one()
    assert writes_after == 0

    # Deleting it again is a clean 404, not a crash.
    r = client.delete(f"/api/v1/chat/sessions/{session_id}", headers={"X-User-Id": user["id"]})
    assert r.status_code == 404
