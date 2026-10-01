"""Tests for the Jev/OpenRouter quality-gate layer (`app/ai/jev_client.py` +
the three checkpoints wired into `app/ai/scorecard_builder.py` — see the module comment
above `MAX_QUALITY_GATE_RETRIES` in that file):

1. KPI-category planning (`_decide_categories_with_gate`, inside `research_kpis`)
2. each category research agent's finding (`_run_research_agent`, right after
   `record_research_finding`)
3. propose_kpis's final per-visit decision (ask_clarification/update_draft/
   update_scoring_formula/respond_conversationally)

Uses `FakeBedrockClient`/`FakeWebSearchClient`/`FakeJevClient` (see tests/fakes.py); no
real network calls (the real OpenRouter/Jev + Bedrock + AgentCore Gateway are only
exercised by the live end-to-end pass, not by pytest — see this task's final report for
that real evidence).

Covers exactly what the task calls out:
- a scripted LOW score at each of the 3 checkpoints triggers the correct bounded-retry
  behavior, and the bounded cap (`MAX_QUALITY_GATE_RETRIES`) prevents an infinite loop —
  proceeding with the best-scoring attempt instead;
- a scripted HIGH score passes through with NO retry (exactly one underlying Bedrock call
  per checkpoint);
- a simulated Jev/OpenRouter failure (the fake's `rate_fn` raising, mirroring the real
  client's `JevUnavailableError` contract) degrades gracefully — the gate is treated as
  passed, and the turn completes normally rather than crashing or hanging.
"""

from __future__ import annotations

import uuid

import pytest

from app.ai import scorecard_builder as sb
from tests.fakes import FakeBedrockClient, FakeJevClient, FakeWebSearchClient, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")

_SINGLE_CATEGORY = [{"name": "Domain research", "focus": "What matters for this domain?"}]

_SUFFICIENT_COVERAGE = tool_use_result(
    "assess_research_coverage",
    {"sufficient": True, "reasoning": "Coverage looks adequate.", "next_categories": []},
)


def _complete_patch() -> dict:
    return {
        "name": "Test Scorecard",
        "purpose": "A test purpose.",
        "domain": "Test domain",
        "audience": "Test audience",
        "target_score": 8,
        "kpis": [
            {
                "name": "KPI A",
                "weight": 100,
                "level": 1,
                "guidelines": {
                    "10": {"qualitative_text": "Great."},
                    "0": {"qualitative_text": "Bad."},
                },
            }
        ],
    }


def _tools_offered(tools) -> set[str]:
    return {t.name for t in (tools or [])}


# --- Checkpoint 1: KPI-category planning ------------------------------------------------


async def test_checkpoint1_low_score_triggers_bounded_retry_then_proceeds() -> None:
    """Jev always scores the decided KPI-category plan low (0.3, below the 0.75
    threshold). `_decide_categories_with_gate` must retry up to
    `MAX_QUALITY_GATE_RETRIES` times (bounded — never hangs) and then proceed with its
    best attempt, and the retry system prompts must genuinely carry Jev's feedback."""
    session_id = str(uuid.uuid4())
    decide_system_prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            decide_system_prompts.append(system or "")
            return tool_use_result("decide_categories", {"categories": _SINGLE_CATEGORY})
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            return tool_use_result(
                "record_research_finding",
                {"summary": "A finding.", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )
        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})
        return tool_use_result("update_draft", {"patch": _complete_patch(), "confirmed": True})

    def rate_fn(*, instruction, answer):
        if answer.startswith("Planned KPI categories:"):
            return 0.3  # checkpoint 1 — always below threshold
        return 1.0  # every other checkpoint passes immediately, isolating this test

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])
    fake_jev = FakeJevClient(rate_fn=rate_fn)

    turn = await sb.start_session(
        session_id,
        "Build me a scorecard for evaluating X.",
        fake_bedrock,
        web_search_client=fake_search,
        jev_client=fake_jev,
    )

    assert turn.status == "confirmed"
    # Bounded: 1 original attempt + MAX_QUALITY_GATE_RETRIES revisions.
    assert len(decide_system_prompts) == sb.MAX_QUALITY_GATE_RETRIES + 1
    # Every retry's system prompt genuinely carries Jev's below-threshold feedback (not a
    # blind re-roll of the identical call).
    for revised_prompt in decide_system_prompts[1:]:
        assert "automated quality check" in revised_prompt
        assert "0.3" in revised_prompt


async def test_checkpoint1_high_score_passes_with_no_retry() -> None:
    """Jev scores the plan well above threshold on the first attempt — no retry happens
    at all (exactly one `decide_categories` call)."""
    session_id = str(uuid.uuid4())
    decide_calls: list[int] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            decide_calls.append(1)
            return tool_use_result("decide_categories", {"categories": _SINGLE_CATEGORY})
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            return tool_use_result(
                "record_research_finding",
                {"summary": "A finding.", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )
        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})
        return tool_use_result("update_draft", {"patch": _complete_patch(), "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])
    fake_jev = FakeJevClient(rate_fn=lambda **kwargs: 0.95)

    turn = await sb.start_session(
        session_id,
        "Build me a scorecard for evaluating X.",
        fake_bedrock,
        web_search_client=fake_search,
        jev_client=fake_jev,
    )

    assert turn.status == "confirmed"
    assert len(decide_calls) == 1


# --- Checkpoint 2: each research agent's finding -------------------------------------------


async def test_checkpoint2_low_score_triggers_bounded_retry_then_proceeds() -> None:
    """Jev always scores the research agent's recorded finding low. The agent must
    re-synthesize up to `MAX_QUALITY_GATE_RETRIES` times (bounded) before returning its
    best attempt to the master for merging, rather than hanging or dropping the category."""
    session_id = str(uuid.uuid4())
    record_calls: list[int] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _SINGLE_CATEGORY})
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            record_calls.append(1)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": "FINDING_MARKER",
                    "suggested_kpis": [],
                    "suggested_thresholds": [],
                    "sources": [],
                },
            )
        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})
        return tool_use_result("update_draft", {"patch": _complete_patch(), "confirmed": True})

    def rate_fn(*, instruction, answer):
        if "FINDING_MARKER" in answer:
            return 0.4  # checkpoint 2 — always below threshold
        return 1.0

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])
    fake_jev = FakeJevClient(rate_fn=rate_fn)

    turn = await sb.start_session(
        session_id,
        "Build me a scorecard for evaluating X.",
        fake_bedrock,
        web_search_client=fake_search,
        jev_client=fake_jev,
    )

    assert turn.status == "confirmed"
    assert len(record_calls) == sb.MAX_QUALITY_GATE_RETRIES + 1
    # The category's finding was still consolidated (not silently dropped) despite never
    # clearing the threshold — see research_findings in the checkpointed state.
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    assert any(f["category"] == _SINGLE_CATEGORY[0]["name"] and f["summary"] == "FINDING_MARKER" for f in findings)


async def test_checkpoint2_high_score_passes_with_no_retry() -> None:
    session_id = str(uuid.uuid4())
    record_calls: list[int] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _SINGLE_CATEGORY})
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            record_calls.append(1)
            return tool_use_result(
                "record_research_finding",
                {"summary": "A great finding.", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )
        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})
        return tool_use_result("update_draft", {"patch": _complete_patch(), "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])
    fake_jev = FakeJevClient(rate_fn=lambda **kwargs: 0.9)

    turn = await sb.start_session(
        session_id,
        "Build me a scorecard for evaluating X.",
        fake_bedrock,
        web_search_client=fake_search,
        jev_client=fake_jev,
    )

    assert turn.status == "confirmed"
    assert len(record_calls) == 1


# --- Checkpoint 3: propose_kpis's final per-visit decision ---------------------------------


async def test_checkpoint3_low_score_triggers_bounded_retry_then_proceeds() -> None:
    """Jev always scores propose_kpis's decision low. The node must revise up to
    `MAX_QUALITY_GATE_RETRIES` times (bounded) before returning its best attempt to the
    user — never hanging or crashing the turn."""
    session_id = str(uuid.uuid4())
    propose_calls: list[int] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        propose_calls.append(1)
        return tool_use_result(
            "ask_clarification",
            {
                "question": "ANSWER_MARKER: what should this scorecard be named?",
                "options": [],
                "missing_fields": ["name"],
            },
        )

    def rate_fn(*, instruction, answer):
        if "ANSWER_MARKER" in answer:
            return 0.2
        return 1.0

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_jev = FakeJevClient(rate_fn=rate_fn)

    # No web_search_client -> research_kpis is a clean no-op (see _web_search_usable),
    # isolating this test to propose_kpis's own checkpoint-3 gate.
    turn = await sb.start_session(
        session_id, "Build me a scorecard for evaluating X.", fake_bedrock, jev_client=fake_jev
    )

    assert turn.status == "awaiting_clarification"
    assert turn.question is not None
    assert "ANSWER_MARKER" in turn.question["question"]
    assert len(propose_calls) == sb.MAX_QUALITY_GATE_RETRIES + 1


async def test_checkpoint3_high_score_passes_with_no_retry() -> None:
    session_id = str(uuid.uuid4())
    propose_calls: list[int] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        propose_calls.append(1)
        return tool_use_result("respond_conversationally", {"response": "Here is some helpful info."})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_jev = FakeJevClient(rate_fn=lambda **kwargs: 0.9)

    turn = await sb.start_session(
        session_id, "Build me a scorecard for evaluating X.", fake_bedrock, jev_client=fake_jev
    )

    assert turn.status == "gathering"
    assert len(propose_calls) == 1


# --- Graceful degradation on a Jev/OpenRouter failure --------------------------------------


async def test_jev_failure_degrades_gracefully_does_not_crash_or_hang_turn() -> None:
    """`FakeJevClient.rate_fn` raising mirrors the real `JevClient.rate_match` contract of
    raising `JevUnavailableError` on any OpenRouter failure. `quality_gate()` must catch
    this and treat the gate as PASSED (graceful degradation) — the turn must complete
    normally, with NO retry spent on a third-party dependency being down."""
    session_id = str(uuid.uuid4())

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("update_draft", {"patch": _complete_patch(), "confirmed": True})

    def rate_fn(*, instruction, answer):
        raise RuntimeError("simulated OpenRouter/Jev outage")

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_jev = FakeJevClient(rate_fn=rate_fn)

    turn = await sb.start_session(
        session_id, "Build me a scorecard for evaluating X.", fake_bedrock, jev_client=fake_jev
    )

    assert turn.status == "confirmed"
    # Exactly ONE underlying Bedrock call for propose_kpis's decision — a degraded
    # (Jev-unreachable) gate passes immediately, spending no retry budget.
    assert len(fake_bedrock.calls) == 1


async def test_no_jev_client_wired_in_is_also_graceful() -> None:
    """Omitting `jev_client` entirely (the default for every call site that doesn't pass
    one — e.g. every other test file in this suite) must behave identically to a live
    Jev failure: gate passed, no retry, no crash. This is what keeps every pre-existing
    scorecard-builder test in this project passing unmodified."""
    session_id = str(uuid.uuid4())

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("update_draft", {"patch": _complete_patch(), "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)

    turn = await sb.start_session(session_id, "Build me a scorecard for evaluating X.", fake_bedrock)

    assert turn.status == "confirmed"
    assert len(fake_bedrock.calls) == 1
