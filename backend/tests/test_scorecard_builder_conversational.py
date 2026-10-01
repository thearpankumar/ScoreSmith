"""Tests for `respond_conversationally` (app/ai/scorecard_builder.py) — the fix for the
architecture gap where every single propose_kpis turn was coerced (via `force_tool_use`)
into either `ask_clarification` (a rigid chip question) or a draft-mutating tool, with no
way for the model to just discuss/explain/answer a question without touching the draft.

Covers exactly what the task calls out:
- a scripted EXPLORATORY question triggers `respond_conversationally` and leaves
  `draft.kpis`/`draft.scoring_formula` completely unchanged
  (`test_exploratory_question_triggers_respond_conversationally_leaves_draft_unchanged`);
- a scripted follow-up DECISION in the NEXT turn correctly triggers `update_draft`
  (`test_followup_decision_after_conversational_turn_updates_draft`);
- an explicit "please look that up" MID-conversation (i.e. on a later human turn, well
  after the once-per-session research fan-out has already run) triggers a real
  `web_search` call outside the initial research phase, and the findings are reported back
  via `respond_conversationally` without mutating the draft
  (`test_on_demand_web_search_mid_conversation_outside_initial_research_phase`);
- the new session-level web_search budget (`MAX_WEB_SEARCH_CALLS_PER_SESSION`) actually
  bounds ad hoc search usage ACROSS human turns, not just within one node visit
  (`test_session_level_web_search_budget_caps_across_turns`).

Uses `FakeBedrockClient`/`FakeWebSearchClient` (see tests/fakes.py); no real network calls
(the real Gateway/Bedrock are only exercised by the live end-to-end pass, not by pytest).
"""

from __future__ import annotations

import uuid

import pytest

from app.ai import scorecard_builder as sb
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, search_result, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")

_COMPLETE_PATCH = {
    "name": "Vendor Risk Review Quality",
    "purpose": "Rate the quality of vendor security risk reviews.",
    "domain": "Vendor Risk Management",
    "audience": "Procurement/security leads",
    "target_score": 8,
    "kpis": [
        {
            "name": "Compliance Coverage",
            "weight": 70,
            "level": 1,
            "guidelines": {
                "10": {"qualitative_text": "Fully covers required controls."},
                "0": {"qualitative_text": "No coverage."},
            },
        },
        {
            "name": "Turnaround Time",
            "weight": 30,
            "level": 1,
            "guidelines": {
                "10": {"qualitative_text": "Reviewed within SLA."},
                "0": {"qualitative_text": "Far outside SLA."},
            },
        },
    ],
}


def _tools_offered(tools) -> set[str]:
    return {t.name for t in (tools or [])}


def _no_dedicated_research_categories(tools) -> tuple[bool, object]:
    """Same helper as test_scorecard_builder_web_search.py — respond to research_kpis's
    own `decide_categories` call (when a web_search_client is wired in) with an empty
    category list, so the rest of the scripted conversation exercises propose_kpis's own
    tool-selection logic in isolation from the separate multi-agent research fan-out."""
    if "decide_categories" in _tools_offered(tools):
        return True, tool_use_result("decide_categories", {"categories": []})
    return False, None


async def test_exploratory_question_triggers_respond_conversationally_leaves_draft_unchanged() -> None:
    """A purely exploratory/informational question ('what are common KPI frameworks for
    vendor risk?') must resolve to respond_conversationally, not ask_clarification or
    update_draft — and the draft must come back byte-for-byte unchanged (still the
    default empty ScorecardDraft)."""
    session_id = str(uuid.uuid4())
    response_text = (
        "Common frameworks for vendor risk scorecards include ISO 27001 control coverage, "
        "SOC 2 attestation review, and NIST 800-161 supply-chain risk criteria. Want me to "
        "build KPIs around one of these?"
    )
    fake = FakeBedrockClient(
        script=[tool_use_result("respond_conversationally", {"response": response_text})]
    )

    turn = await sb.start_session(
        session_id, "What are some common KPI frameworks for vendor risk scorecards?", fake
    )

    # respond_conversationally must NOT be surfaced as a clarifying question (no chip UI)
    # and must not have advanced the draft in any way.
    assert turn.question is None
    assert turn.assistant_note == response_text
    assert turn.draft["kpis"] == []
    assert turn.draft["scoring_formula"] is None
    assert turn.draft["name"] is None
    assert turn.status == "gathering"

    # White-box: the graph is genuinely paused (interrupted), not silently stuck mid-run —
    # proves this reuses the same crash/restart-safe interrupt mechanism as
    # ask_clarification, just with a different payload "kind".
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    assert snapshot.interrupts
    assert snapshot.interrupts[0].value.get("kind") == "conversational_response"
    assert snapshot.interrupts[0].value.get("response") == response_text


async def test_followup_decision_after_conversational_turn_updates_draft() -> None:
    """After a conversational (non-mutating) turn, a genuine decision on the NEXT turn
    ('yes, use the ISO 27001 one — build it out') must resolve to a real update_draft that
    actually changes the draft — proving the two tools are reliably distinguished turn to
    turn, not just within a single scripted call."""
    session_id = str(uuid.uuid4())
    fake1 = FakeBedrockClient(
        script=[
            tool_use_result(
                "respond_conversationally",
                {"response": "Common options include ISO 27001, SOC 2, and NIST 800-161."},
            )
        ]
    )
    turn1 = await sb.start_session(session_id, "What are common vendor risk frameworks?", fake1)
    assert turn1.status == "gathering"
    assert turn1.draft["kpis"] == []

    fake2 = FakeBedrockClient(
        script=[tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})]
    )
    turn2 = await sb.send_message(
        session_id, "Yes, let's go with the ISO 27001 approach — build it out and save it.", fake2
    )

    assert turn2.status == "confirmed"
    assert [k["name"] for k in turn2.draft["kpis"]] == ["Compliance Coverage", "Turnaround Time"]
    # Exactly one Bedrock call was needed to resolve the decision turn (resuming the
    # conversational_response interrupt re-enters propose_kpis directly, with a fresh
    # llm_turn_count budget — no wasted extra turns).
    assert len(fake2.calls) == 1


async def test_on_demand_web_search_mid_conversation_outside_initial_research_phase() -> None:
    """web_search must be reachable on a LATER human turn (well after research_kpis's own
    once-per-session fan-out has already run and set research_done=True), when the user
    explicitly asks the assistant to look something up — and the finding must be reported
    back via respond_conversationally, not silently folded into a draft mutation."""
    session_id = str(uuid.uuid4())

    def turn1_converse_fn(*, messages, system, tools, force_tool_use, model_id):
        handled, response = _no_dedicated_research_categories(tools)
        if handled:
            return response
        # First real propose_kpis call this session: just ask a clarifying question,
        # deliberately WITHOUT touching web_search yet, so turn 2 below is unambiguously
        # "mid-conversation", not a continuation of the same node visit.
        return tool_use_result(
            "ask_clarification",
            {"question": "What's the primary purpose of this scorecard?", "options": [], "missing_fields": ["purpose"]},
        )

    fake_search = FakeWebSearchClient()
    fake1 = FakeBedrockClient(converse_fn=turn1_converse_fn)
    turn1 = await sb.start_session(
        session_id,
        "I want a vendor risk review scorecard.",
        fake1,
        web_search_client=fake_search,
    )
    assert turn1.status == "awaiting_clarification"
    assert fake_search.queries == []  # nothing searched yet — proves turn 2 is genuinely new

    draft_before = turn1.draft
    propose_call_log: list[int] = []

    def turn2_converse_fn(*, messages, system, tools, force_tool_use, model_id):
        propose_call_log.append(1)
        if len(propose_call_log) == 1:
            assert "web_search" in _tools_offered(tools), (
                "web_search must be offered on a later human turn, not only the session's "
                "first — this is the on-demand mid-conversation search capability."
            )
            return tool_use_result("web_search", {"query": "current PCI DSS vendor review benchmark 2026"})
        joined = " ".join(block.get("text", "") for m in messages for block in m.get("content", []))
        assert "PCI DSS" in joined or "pci dss" in joined.lower()
        assert "respond_conversationally" in _tools_offered(tools)
        return tool_use_result(
            "respond_conversationally",
            {"response": "I found a current PCI DSS vendor review benchmark — want me to add it as a KPI?"},
        )

    fake2 = FakeBedrockClient(converse_fn=turn2_converse_fn)
    fake_search_script = FakeWebSearchClient(
        script=[[search_result("PCI SSC Vendor Review Guidance", "https://pcisecuritystandards.org", "...")]]
    )

    turn2 = await sb.send_message(
        session_id,
        "Before we continue — can you look up the current PCI DSS vendor review benchmark?",
        fake2,
        web_search_client=fake_search_script,
    )

    assert fake_search_script.queries == ["current PCI DSS vendor review benchmark 2026"]
    assert turn2.status == "gathering"
    assert turn2.question is None
    assert "PCI DSS" in turn2.assistant_note
    # The draft must be completely untouched by this on-demand-research-then-report turn.
    assert turn2.draft == draft_before


async def test_session_level_web_search_budget_caps_across_turns() -> None:
    """MAX_WEB_SEARCH_CALLS_PER_SESSION must bound propose_kpis's own ad hoc web_search
    usage ACROSS human turns, not just within a single node visit (which
    MAX_WEB_SEARCH_CALLS_PER_PROPOSE already bounds) — otherwise a long-running session
    could rack up unbounded real Gateway calls one human turn at a time."""
    session_id = str(uuid.uuid4())
    assert sb.MAX_WEB_SEARCH_CALLS_PER_SESSION < 100  # sanity: a real, small bound exists

    # Drain the session budget across several human turns, each doing exactly one search
    # then stopping via respond_conversationally (so each turn is cheap/short to script).
    def make_converse_fn(query_label: str, expect_search: bool):
        state = {"n": 0}

        def converse_fn(*, messages, system, tools, force_tool_use, model_id):
            handled, response = _no_dedicated_research_categories(tools)
            if handled:
                return response
            state["n"] += 1
            if state["n"] == 1:
                offered = _tools_offered(tools)
                if expect_search:
                    assert "web_search" in offered
                    return tool_use_result("web_search", {"query": query_label})
                assert "web_search" not in offered, (
                    "web_search must no longer be offered once the session-level budget "
                    "(MAX_WEB_SEARCH_CALLS_PER_SESSION) is exhausted."
                )
            return tool_use_result("respond_conversationally", {"response": "Noted."})

        return converse_fn

    fake_search = FakeWebSearchClient(search_fn=lambda q: [search_result("T", "https://x.example", "s")])

    # First turn: research fan-out skipped (no categories), one ad hoc web_search happens, then
    # a plain conversational close — establishes web_search_calls_used > 0 in state.
    fake1 = FakeBedrockClient(converse_fn=make_converse_fn("q1", expect_search=True))
    turn1 = await sb.start_session(session_id, "Vendor risk scorecard.", fake1, web_search_client=fake_search)
    assert turn1.status == "gathering"

    # Drive the session-level counter to (and past) the cap via repeated follow-up turns,
    # each doing one more search — resuming the conversational_response interrupt each
    # time (mirrors test_followup_decision_after_conversational_turn_updates_draft).
    remaining_budget = sb.MAX_WEB_SEARCH_CALLS_PER_SESSION - 1  # turn 1 already used 1
    turns_needed = remaining_budget + 1  # one extra turn to prove the cap actually bites
    for i in range(turns_needed):
        is_last = i == turns_needed - 1
        fake = FakeBedrockClient(converse_fn=make_converse_fn(f"q{i + 2}", expect_search=not is_last))
        turn = await sb.send_message(
            session_id, f"Also look up thing #{i + 2} for me.", fake, web_search_client=fake_search
        )
        assert turn.status == "gathering"

    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    assert snapshot.values["web_search_calls_used"] == sb.MAX_WEB_SEARCH_CALLS_PER_SESSION
