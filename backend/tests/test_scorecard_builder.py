"""Tests for the LangGraph scorecard-builder state machine (app/ai/scorecard_builder.py),
against a **real** Postgres `AsyncPostgresSaver` checkpointer (no mocks for Postgres) with
a **fake** Bedrock client (no real Bedrock credentials in this environment — see
tests/fakes.py and the AI core's final report for what that distinction means here).

Covers exactly what the task calls out: a bad `update_draft` patch is rejected without
corrupting draft state, and state survives a *simulated* interrupt/resume across a fresh
Postgres connection (standing in for a server restart).
"""

from __future__ import annotations

import uuid

import pytest

from app.ai import scorecard_builder as sb
from tests.fakes import FakeBedrockClient, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")

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


async def test_llm_first_propose_then_confirm_in_one_turn() -> None:
    """'LLM first, human second': the model can go straight from a fresh draft to a
    complete, confirmed one via a single update_draft call, with no clarification asked."""
    session_id = str(uuid.uuid4())
    fake = FakeBedrockClient(
        script=[tool_use_result("update_draft", {"patch": _COMPLETE_DRAFT_PATCH, "confirmed": True})]
    )

    turn = await sb.start_session(session_id, "Build me a support-ticket quality scorecard.", fake)

    assert turn.status == "confirmed"
    assert turn.draft["purpose"] == "Rate the quality of support ticket resolutions."
    assert [k["name"] for k in turn.draft["kpis"]] == ["Accuracy", "Tone"]


async def test_ask_clarification_pauses_then_resumes_with_answer() -> None:
    session_id = str(uuid.uuid4())
    fake1 = FakeBedrockClient(
        script=[
            tool_use_result(
                "ask_clarification",
                {"question": "What is this scorecard's purpose?", "options": [], "missing_fields": ["purpose"]},
            )
        ]
    )

    turn1 = await sb.start_session(session_id, "I want to build a scorecard.", fake1)
    assert turn1.status == "awaiting_clarification"
    assert turn1.question["question"] == "What is this scorecard's purpose?"
    assert turn1.question["missing_fields"] == ["purpose"]

    # A non-confirming update_draft always loops straight back to propose_kpis for
    # another LLM turn (so the model can chain several tool calls before a human is
    # prompted again — see scorecard_builder.py's MAX_LLM_TURNS_PER_HUMAN_TURN docs), so
    # the natural next call here is the model asking about the next missing field.
    fake2 = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {"patch": {"purpose": "Rate support ticket resolutions."}, "confirmed": False},
            ),
            tool_use_result(
                "ask_clarification",
                {"question": "What domain is this for?", "options": [], "missing_fields": ["domain"]},
            ),
        ]
    )
    turn2 = await sb.send_message(session_id, "Rate support ticket resolutions.", fake2)
    assert turn2.status == "awaiting_clarification"
    assert turn2.question["question"] == "What domain is this for?"
    # The purpose from the first update_draft call was committed even though the graph
    # immediately continued on to ask another question in the same turn.
    assert turn2.draft["purpose"] == "Rate support ticket resolutions."


async def test_invalid_update_draft_patch_is_rejected_without_corrupting_draft() -> None:
    """A patch with an out-of-range weight must never be committed to the draft — the
    model should instead see a validation error and get another turn."""
    session_id = str(uuid.uuid4())
    fake = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {"patch": {"kpis": [{"name": "Accuracy", "weight": 999}]}, "confirmed": False},
            ),
            tool_use_result(
                "ask_clarification",
                {"question": "What weight should Accuracy have?", "options": [], "missing_fields": ["kpis"]},
            ),
        ]
    )

    turn = await sb.start_session(session_id, "Build me a scorecard.", fake)

    # The bad patch was rejected: update_draft looped back to propose_kpis, which (per
    # the script) then asked a clarifying question instead — proving the graph never
    # treated the invalid patch as committed.
    assert turn.status == "awaiting_clarification"
    assert turn.draft["kpis"] == []  # still empty — the weight=999 patch never landed

    # White-box check: the rejected patch's validation error was recorded in state for
    # the model's next turn to see (not silently swallowed).
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    assert snapshot.values["last_patch_error"] is not None
    assert "weight" in snapshot.values["last_patch_error"] or "kpis" in snapshot.values["last_patch_error"]


async def test_state_survives_simulated_restart() -> None:
    """Closes the pooled AsyncPostgresSaver connection and drops the module-level graph
    singleton — standing in for a real process restart — then resumes from a brand new
    connection using only the session_id, per the plan's "session survives restarts;
    client only needs to hold session_id" requirement."""
    session_id = str(uuid.uuid4())
    fake1 = FakeBedrockClient(
        script=[
            tool_use_result(
                "ask_clarification",
                {"question": "What is the purpose?", "options": [], "missing_fields": ["purpose"]},
            )
        ]
    )
    turn1 = await sb.start_session(session_id, "Build a scorecard.", fake1)
    assert turn1.status == "awaiting_clarification"

    await sb.get_graph_manager().aclose()
    sb._graph_manager = None  # force a brand-new AsyncPostgresSaver connection next call

    fake2 = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft", {"patch": {"purpose": "Rate things well."}, "confirmed": False}
            ),
            tool_use_result(
                "ask_clarification",
                {"question": "What domain?", "options": [], "missing_fields": ["domain"]},
            ),
        ]
    )
    turn2 = await sb.send_message(session_id, "Rate things well.", fake2)

    assert turn2.status == "awaiting_clarification"
    assert turn2.draft["purpose"] == "Rate things well."


async def test_get_session_state_reflects_pending_question_without_advancing() -> None:
    session_id = str(uuid.uuid4())
    fake = FakeBedrockClient(
        script=[
            tool_use_result(
                "ask_clarification",
                {"question": "Q?", "options": ["a", "b"], "missing_fields": ["purpose"]},
            )
        ]
    )
    await sb.start_session(session_id, "hello", fake)

    state = await sb.get_session_state(session_id)
    assert state is not None
    assert state.status == "awaiting_clarification"
    assert state.question["options"] == ["a", "b"]
