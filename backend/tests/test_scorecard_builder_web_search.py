"""Tests for the `web_search` tool integration in `propose_kpis`
(app/ai/scorecard_builder.py) — the bounded search-loop + forced-closure pattern, and
that a failing/empty search never breaks the chat turn. Uses `FakeBedrockClient` +
`FakeWebSearchClient` (see tests/fakes.py); no real network calls (the real Gateway is
only exercised by the live end-to-end pass, not by `pytest`).

Since `research_kpis` (see app/ai/scorecard_builder.py) now runs once, ahead of
`propose_kpis`, on any session wired with a `web_search_client`, every `converse_fn` below
dispatches on the OFFERED TOOL NAMES rather than call position/count — robust to that extra
"decide research angles" call, and clearer about which node's turn is being scripted.
Tests here script `decide_research_angles` to return NO angles (`angles: []`), i.e. "this
domain doesn't need a dedicated research fan-out" — deliberately isolating and exercising
propose_kpis's own single-agent ad hoc `web_search` loop, unrelated to the fan-out itself
(see test_scorecard_builder_research.py for the fan-out's own concurrency/grounding/
graceful-degradation tests).
"""

from __future__ import annotations

import uuid

import pytest

from app.ai import scorecard_builder as sb
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, search_result, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")

_COMPLETE_PATCH = {
    "name": "Incident Postmortem Quality",
    "purpose": "Rate the quality of incident postmortem reports.",
    "domain": "Site Reliability Engineering",
    "audience": "SRE leads",
    "target_score": 8,
    "kpis": [
        {
            "name": "Time to Detect",
            "weight": 100,
            "level": 1,
            "guidelines": {
                "10": {
                    "qualitative_text": "Detected within industry-benchmark MTTD.",
                    "quantitative_criteria": {"mttd_minutes_max": 5},
                },
                "0": {"qualitative_text": "Detected far too late."},
            },
        },
    ],
}


def _tools_offered(tools) -> set[str]:
    return {t.name for t in (tools or [])}


def _no_dedicated_research_angles(tools) -> tuple[bool, object]:
    """If `decide_research_angles` is being offered (research_kpis's own call), respond
    with an empty angle list — "no dedicated fan-out needed for this domain" — so the rest
    of the scripted conversation exercises propose_kpis's own ad hoc web_search loop in
    isolation. Returns (handled, response)."""
    if "decide_research_angles" in _tools_offered(tools):
        return True, tool_use_result("decide_research_angles", {"angles": []})
    return False, None


async def test_web_search_tool_offered_and_results_fed_back_into_proposal() -> None:
    """The model calls web_search once, the (fake) results are genuinely visible in its
    next turn's message context, and it then proposes a complete draft via update_draft
    — proving the tool is bound in propose_kpis and its results actually reach the
    model, not just that the call happened."""
    session_id = str(uuid.uuid4())
    propose_call_log: list[dict] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        handled, response = _no_dedicated_research_angles(tools)
        if handled:
            return response
        propose_call_log.append({"messages": messages, "tools": tools})
        if len(propose_call_log) == 1:
            assert "web_search" in _tools_offered(tools)
            return tool_use_result(
                "web_search", {"query": "incident postmortem MTTD industry benchmark"}
            )
        joined = " ".join(block.get("text", "") for m in messages for block in m.get("content", []))
        assert "MTTD" in joined or "postmortem" in joined.lower()
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(
        script=[
            [
                search_result(
                    title="Google SRE Book: Postmortem Culture",
                    url="https://sre.google/sre-book/postmortem-culture/",
                    snippet="Median time-to-detect (MTTD) for well-instrumented services is under 5 minutes.",
                )
            ]
        ]
    )

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate incident postmortem report quality.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"
    assert fake_search.queries == ["incident postmortem MTTD industry benchmark"]
    assert len(propose_call_log) == 2


async def test_web_search_not_offered_when_no_client_wired() -> None:
    """Backward compatibility: with no web_search_client (the default), the tool is never
    offered — and research_kpis is a no-op with no extra Bedrock call at all (it gates on
    web_search_client just like propose_kpis's own ad hoc tool does) — so behavior is
    identical to before either feature existed."""
    session_id = str(uuid.uuid4())

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        assert "web_search" not in _tools_offered(tools)
        assert "decide_research_angles" not in _tools_offered(tools)
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    turn = await sb.start_session(session_id, "Build me a scorecard.", fake_bedrock)
    assert turn.status == "confirmed"
    assert len(fake_bedrock.calls) == 1


async def test_bounded_search_loop_forces_closure_after_budget() -> None:
    """A model that keeps calling web_search is forced to stop once
    MAX_WEB_SEARCH_CALLS_PER_PROPOSE searches have been made: web_search then drops out
    of the offered tool set (force_tool_use means it must pick one of the remaining
    two), so this fake's own logic falls back to update_draft. Asserting the search
    client saw exactly the budget's worth of queries — not more — proves the loop is
    actually bounded, not just eventually terminating for unrelated reasons."""
    session_id = str(uuid.uuid4())

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        handled, response = _no_dedicated_research_angles(tools)
        if handled:
            return response
        if "web_search" in _tools_offered(tools):
            return tool_use_result("web_search", {"query": "kpi benchmark"})
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [search_result("T", "https://x.example", "s")])

    turn = await sb.start_session(
        session_id, "Build me a scorecard.", fake_bedrock, web_search_client=fake_search
    )

    assert turn.status == "confirmed"
    assert len(fake_search.queries) == sb.MAX_WEB_SEARCH_CALLS_PER_PROPOSE

    # llm_turn_count increments once per propose_kpis NODE VISIT, regardless of how many
    # web_search sub-calls happened inside that single visit's inner loop, and regardless
    # of research_kpis's own (separate) decide-angles call — see MAX_LLM_TURNS_PER_HUMAN_TURN
    # docs in scorecard_builder.py for why research_kpis never touches this counter.
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    assert snapshot.values["llm_turn_count"] == 1


async def test_failed_web_search_returns_empty_and_does_not_break_turn() -> None:
    """FakeWebSearchClient returning no results (what a real Gateway failure looks like
    to the caller — the real client's own contract is "never raise, worst case []", see
    app/ai/web_search.py) must not break the chat turn; the graph proceeds normally."""
    session_id = str(uuid.uuid4())
    propose_call_log: list[dict] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        handled, response = _no_dedicated_research_angles(tools)
        if handled:
            return response
        propose_call_log.append(1)
        if len(propose_call_log) == 1:
            return tool_use_result("web_search", {"query": "nonexistent benchmark xyz"})
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(script=[[]])  # empty results — simulated failure

    turn = await sb.start_session(
        session_id, "Build me a scorecard.", fake_bedrock, web_search_client=fake_search
    )

    assert turn.status == "confirmed"
    assert fake_search.queries == ["nonexistent benchmark xyz"]


async def test_web_search_client_raising_unexpectedly_is_swallowed() -> None:
    """Belt-and-suspenders: even if a web_search_client implementation violates its own
    "never raises" contract, propose_kpis's own try/except must stop that exception from
    killing the chat turn."""
    session_id = str(uuid.uuid4())
    propose_call_log: list[dict] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        handled, response = _no_dedicated_research_angles(tools)
        if handled:
            return response
        propose_call_log.append(1)
        if len(propose_call_log) == 1:
            return tool_use_result("web_search", {"query": "boom"})
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    def _raising_search_fn(query: str):
        raise RuntimeError("simulated unexpected failure")

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=_raising_search_fn)

    turn = await sb.start_session(
        session_id, "Build me a scorecard.", fake_bedrock, web_search_client=fake_search
    )

    assert turn.status == "confirmed"
    assert fake_search.queries == ["boom"]  # the raising call genuinely happened and was swallowed
    assert len(propose_call_log) == 2  # web_search attempt, then the fallback update_draft
