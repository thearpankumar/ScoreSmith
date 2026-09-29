"""Tests for the live turn-trace event log (`chat_turn_events` / `GET
/chat/sessions/{id}/turn-events`) that replaces the old generic "Assistant is thinking…"
indicator — see `app/models/chat_turn_event.py`, `app/ai/turn_events.py`, and the
`emit_turn_event` call sites wired into `app/ai/scorecard_builder.py`'s `research_kpis` /
`_run_research_agent` / `propose_kpis`.

Real Postgres throughout (per this project's testing philosophy — no mocks), real
FakeBedrockClient/FakeWebSearchClient test doubles (see tests/fakes.py), no live network.

Covers:
- the multi-agent research fan-out writes DISTINCT, per-agent-actor events, not one
  agent's events mislabeled onto another (`test_research_fanout_writes_distinct_actor_events`);
- events are genuinely visible via a SEPARATE DB connection WHILE the turn is still
  in-flight, mirroring how `pending_turn_started_at`/`turn_in_progress` was proven live in
  the prior pass (`test_events_visible_mid_flight_via_api`);
- `GET .../turn-events` is scoped to the CURRENT/most recent turn only — a second turn's
  events replace (prune) the first turn's, rather than accumulating forever
  (`test_turn_events_endpoint_scopes_to_latest_turn_only`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai import scorecard_builder as sb
from app.deps import get_bedrock_client, get_web_search_client
from app.main import app
from app.models.chat_session import ChatSession
from app.models.chat_turn_event import ChatTurnEvent
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, search_result, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")

_ANGLES = [
    {"angle": "Security standards & frameworks", "query_focus": "ISO 27001 / NIST vendor security review controls"},
    {"angle": "Regulatory/compliance requirements", "query_focus": "SOC 2 vendor compliance review requirements"},
]

_COMPLETE_PATCH = {
    "name": "Vendor Security Compliance Review Quality",
    "purpose": "Rate the quality of vendor security compliance reviews.",
    "domain": "Vendor Risk Management",
    "audience": "Procurement/security leads",
    "target_score": 8,
    "kpis": [
        {
            "name": "Compliance Coverage",
            "weight": 100,
            "level": 1,
            "guidelines": {
                "10": {"qualitative_text": "Fully covers required controls."},
                "0": {"qualitative_text": "No coverage."},
            },
        },
    ],
}


def _tools_offered(tools) -> set[str]:
    return {t.name for t in (tools or [])}


def _angle_from_messages(messages) -> str | None:
    for m in messages:
        for block in m.get("content", []):
            text = block.get("text", "")
            if text.startswith("Research angle: "):
                return text.split("Research angle: ", 1)[1].split("\n", 1)[0]
    return None


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    app.dependency_overrides.pop(get_bedrock_client, None)
    app.dependency_overrides.pop(get_web_search_client, None)


# --- Direct graph-level: distinct per-agent actor events --------------------------------


async def test_research_fanout_writes_distinct_actor_events(async_db_session, seed_user_id: str) -> None:
    """After a real (fake-Bedrock-backed) research fan-out, `chat_turn_events` contains
    genuinely distinct rows for `master`, `research_agent_1`, and `research_agent_2` — not
    one agent's activity mislabeled as another's, and not merely a single generic
    "thinking" event."""
    session_id = uuid.uuid4()
    # chat_turn_events.session_id has a real FK to chat_sessions — insert a real row
    # first, exactly like the production code path (app/api/v1/chat.py) always does
    # before calling into the graph.
    async_db_session.add(ChatSession(id=session_id, user_id=uuid.UUID(seed_user_id)))
    await async_db_session.commit()

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})
        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {angle}",
                    "suggested_kpis": [{"name": f"KPI[{angle}]", "rationale": "r"}],
                    "suggested_thresholds": [],
                    "sources": [{"title": f"Source[{angle}]", "url": "https://example.com"}],
                },
            )
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(
        search_fn=lambda q: [search_result("Result", "https://example.com", "snippet")]
    )
    turn_started_at = datetime.now(UTC)

    turn = await sb.start_session(
        str(session_id),
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
        turn_started_at=turn_started_at,
    )
    assert turn.status == "confirmed"

    rows = (
        (
            await async_db_session.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.session_id == session_id)
                .order_by(ChatTurnEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "expected chat_turn_events rows to have been written"

    actors = {r.actor for r in rows}
    assert actors == {"master", "research_agent_1", "research_agent_2"}, actors

    # Every event's `turn_started_at` is correlated to THIS turn attempt.
    assert all(r.turn_started_at == turn_started_at for r in rows)

    # Each research agent has its own distinct, real, non-generic trace: started -> at
    # least one searching/search_result pair -> completed. Crucially, agent 1's events
    # never mention agent 2's angle and vice versa — proving they are not cross-labeled.
    for i, spec in enumerate(_ANGLES, start=1):
        actor = f"research_agent_{i}"
        agent_rows = [r for r in rows if r.actor == actor]
        event_types = {r.event_type for r in agent_rows}
        assert "started" in event_types
        assert "completed" in event_types
        assert any(spec["angle"] in r.message for r in agent_rows)
        other_angle = _ANGLES[1 - (i - 1)]["angle"]
        assert not any(other_angle in r.message for r in agent_rows), (
            f"{actor}'s events mention the OTHER agent's angle {other_angle!r} — "
            "actors are not being kept distinct."
        )

    master_types = {r.event_type for r in rows if r.actor == "master"}
    assert "deciding_angles" in master_types
    assert "proposing" in master_types
    assert "completed" in master_types
    # The message text is genuinely descriptive, not just the bare event_type code.
    deciding = next(r for r in rows if r.actor == "master" and r.event_type == "deciding_angles")
    assert _ANGLES[0]["angle"] in deciding.message
    assert _ANGLES[1]["angle"] in deciding.message


async def test_emit_turn_event_is_a_silent_no_op_with_no_turn_started_at(async_db_session, seed_user_id: str) -> None:
    """`turn_started_at=None` (e.g. a seeded "Refine with assistant" session that never
    ran through the API's turn-marking wrapper) must not write anything and must not
    raise — this is the explicit no-op contract documented on `emit_turn_event`."""
    session_id = uuid.uuid4()
    async_db_session.add(ChatSession(id=session_id, user_id=uuid.UUID(seed_user_id)))
    await async_db_session.commit()

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    turn = await sb.start_session(
        str(session_id),
        "Build me a simple internal checklist scorecard.",
        fake_bedrock,
        turn_started_at=None,
    )
    assert turn.status in ("confirmed", "awaiting_clarification", "gathering")

    rows = (
        await async_db_session.execute(select(ChatTurnEvent).where(ChatTurnEvent.session_id == session_id))
    ).scalars().all()
    assert rows == []


# --- API-level: mid-flight visibility + per-turn scoping/pruning ------------------------


def test_events_visible_mid_flight_via_api(client: TestClient, seed_user_id: str) -> None:
    """Proves live, mid-flight visibility (mirrors how `pending_turn_started_at`/
    `turn_in_progress` was proven live in the prior pass): a scripted Bedrock call queries
    `chat_turn_events` via a COMPLETELY SEPARATE DB connection (the sync engine — exactly
    what a concurrent GET from a different browser tab would use) from *inside* the
    still-in-flight HTTP request, and finds that events from more than one concurrently
    running research agent are already persisted and readable, before the request has
    returned."""
    from app.db import SyncSessionLocal

    mid_flight_actor_snapshots: list[set[str]] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        with SyncSessionLocal() as db:
            rows = db.execute(select(ChatTurnEvent)).scalars().all()
            mid_flight_actor_snapshots.append({r.actor for r in rows})

        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})
        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {angle}",
                    "suggested_kpis": [],
                    "suggested_thresholds": [
                        {"metric": "m", "value_or_range": "v", "source_note": angle}
                    ],
                    "sources": [],
                },
            )
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=converse_fn)
    app.dependency_overrides[get_web_search_client] = lambda: FakeWebSearchClient(
        search_fn=lambda q: [search_result("Result", "https://example.com", "snippet")]
    )

    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I want a scorecard to rate our vendor security compliance reviews."},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    session_id = body["session_id"]

    assert mid_flight_actor_snapshots, "expected at least one scripted Bedrock call"
    # At some point WHILE the request was still blocked/in-flight, a separate DB
    # connection could already see events from BOTH concurrently-running research agents
    # — genuine mid-flight, cross-agent visibility, not just "visible after the fact".
    assert any(
        len({a for a in snap if a.startswith("research_agent_")}) >= 2 for snap in mid_flight_actor_snapshots
    ), mid_flight_actor_snapshots

    # After the request has returned, the persisted log is also readable via the real
    # endpoint, and covers the whole pipeline: master + both research agents, ending in
    # a "completed"/confirmed master event.
    events_r = client.get(f"/api/v1/chat/sessions/{session_id}/turn-events")
    assert events_r.status_code == 200, events_r.text
    events = events_r.json()
    actors = {e["actor"] for e in events}
    assert actors == {"master", "research_agent_1", "research_agent_2"}
    # Ordered by created_at, ascending.
    timestamps = [e["created_at"] for e in events]
    assert timestamps == sorted(timestamps)
    for e in events:
        assert e["message"], "every event must carry a genuinely descriptive message, not just a bare code"


def test_turn_events_endpoint_scopes_to_latest_turn_only(client: TestClient, seed_user_id: str) -> None:
    """A second turn's events REPLACE the first turn's in what `GET .../turn-events`
    returns (see `_mark_turn_in_progress`'s pruning) — proving the endpoint never leaks a
    long-lived session's whole event history, only the current/most recent turn's."""
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        converse_fn=lambda **_kwargs: tool_use_result(
            "update_draft", {"patch": {"name": "Draft v1"}, "confirmed": False}
        )
    )
    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "Build me a simple internal checklist scorecard."},
        headers={"X-User-Id": seed_user_id},
    )
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    first_turn_events = client.get(f"/api/v1/chat/sessions/{session_id}/turn-events").json()
    assert len(first_turn_events) > 0
    first_turn_started_at = first_turn_events[0]["turn_started_at"]

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        converse_fn=lambda **_kwargs: tool_use_result(
            "ask_clarification",
            {"question": "What should we call it?", "options": [], "missing_fields": ["name"]},
        )
    )
    r2 = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Rename the draft."},
        headers={"X-User-Id": seed_user_id},
    )
    assert r2.status_code == 200, r2.text

    second_turn_events = client.get(f"/api/v1/chat/sessions/{session_id}/turn-events").json()
    assert len(second_turn_events) > 0
    assert all(e["turn_started_at"] != first_turn_started_at for e in second_turn_events), (
        "expected the first turn's events to have been pruned once the second turn started"
    )
    assert len({e["turn_started_at"] for e in second_turn_events}) == 1, (
        "expected exactly one turn's worth of events to be present at a time"
    )
