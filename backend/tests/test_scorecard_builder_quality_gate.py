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
from tests.fakes import FakeBedrockClient, FakeJevClient, FakeWebSearchClient, text_result, tool_use_result

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
    critique_system_prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "critique_response" in names:
            critique_system_prompts.append(system or "")
            return tool_use_result(
                "critique_response",
                {
                    "specific_problems": ["CRITIQUE_MARKER: the category names are too abstract."],
                    "concrete_fix": "Use business-recognizable category names instead.",
                },
            )
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
    # Every retry's system prompt genuinely carries the ADVISOR'S concrete critique (not a
    # blind re-roll of the identical call, and not the old generic "try harder" note).
    for revised_prompt in decide_system_prompts[1:]:
        assert "automated quality check" in revised_prompt
        assert "0.3" in revised_prompt
    # The advisor node itself: exactly one critique call per failed attempt with a retry
    # left (never JEV/OpenRouter — see jev_client.py; it's a plain Bedrock converse call
    # like everything else in this module), and the SECOND one carries the first critique
    # plus the prior output so it can refine rather than repeat the same advice.
    assert len(critique_system_prompts) == sb.MAX_QUALITY_GATE_RETRIES
    assert "Planned KPI categories:" in critique_system_prompts[0]
    assert "0.30" in critique_system_prompts[0]
    assert "SECOND FAILURE" in critique_system_prompts[1]
    assert "CRITIQUE_MARKER" in critique_system_prompts[1]


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
    critique_system_prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "critique_response" in names:
            critique_system_prompts.append(system or "")
            return tool_use_result(
                "critique_response",
                {
                    "specific_problems": ["CRITIQUE_MARKER: the finding has no concrete thresholds."],
                    "concrete_fix": "Search for a real published benchmark and cite it.",
                },
            )
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
    # The advisor/critique node: exactly one call per failed attempt with a retry left, and
    # the SECOND carries the first critique + prior output (see module comment above
    # MAX_QUALITY_GATE_RETRIES in scorecard_builder.py).
    assert len(critique_system_prompts) == sb.MAX_QUALITY_GATE_RETRIES
    assert "FINDING_MARKER" in critique_system_prompts[0]
    assert "0.40" in critique_system_prompts[0]
    assert "SECOND FAILURE" in critique_system_prompts[1]
    assert "CRITIQUE_MARKER" in critique_system_prompts[1]
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
    user — never hanging or crashing the turn. Also covers the new advisor/critique step
    (see the module comment above MAX_QUALITY_GATE_RETRIES in scorecard_builder.py): each
    failed gate attempt must trigger exactly one `critique_response` advisor call (same
    Bedrock client, never Jev) BEFORE the next retry, and the advisor's own system prompt
    must carry the previous critique + prior output on the SECOND failure."""
    session_id = str(uuid.uuid4())
    propose_calls: list[int] = []
    critique_system_prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "critique_response" in names:
            critique_system_prompts.append(system or "")
            return tool_use_result(
                "critique_response",
                {
                    "specific_problems": ["CRITIQUE_MARKER: the question ignored the user's actual request."],
                    "concrete_fix": "Ask about the scorecard's purpose instead.",
                },
            )
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
    # Exactly one advisor/critique call per failed attempt that still has a retry left
    # (never after the FINAL attempt — that's the bounded-cap path, no point critiquing an
    # attempt nothing will ever read).
    assert len(critique_system_prompts) == sb.MAX_QUALITY_GATE_RETRIES
    # The advisor's own call carries the real task + the real failing output + the real
    # score — not a generic prompt.
    assert "ANSWER_MARKER" in critique_system_prompts[0]
    assert "0.20" in critique_system_prompts[0]
    # On the SECOND failure, the advisor also sees what the first critique suggested and
    # what the response looked like before that critique was applied (so it can refine
    # rather than repeat the same advice) — see _generate_quality_gate_critique.
    assert "SECOND FAILURE" in critique_system_prompts[1]
    assert "CRITIQUE_MARKER" in critique_system_prompts[1]


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


# --- The advisor/critique node itself (_generate_quality_gate_critique) --------------------


async def test_critique_call_uses_same_bedrock_client_never_jev() -> None:
    """The advisor/critique step must be a plain `bedrock.converse(force_tool_use=True)`
    call on the SAME GLM-5 client as the rest of the pipeline — never Jev/OpenRouter, which
    stays a pure scorer (see jev_client.py's own module docstring). A `FakeJevClient` whose
    `rate_fn` only ever returns a float proves Jev itself is never asked to produce text."""
    fake_jev = FakeJevClient(rate_fn=lambda **kwargs: 0.1)
    fake_bedrock = FakeBedrockClient(
        converse_fn=lambda **kwargs: tool_use_result(
            "critique_response",
            {"specific_problems": ["too vague"], "concrete_fix": "be specific about X"},
        )
    )

    critique = await sb._generate_quality_gate_critique(
        fake_bedrock,
        None,
        task_context="Do the task.",
        produced_output="A vague answer.",
        gate_score=0.1,
    )

    assert len(fake_bedrock.calls) == 1
    assert fake_bedrock.calls[0]["tools"][0].name == "critique_response"
    assert len(fake_jev.calls) == 0
    assert "too vague" in critique
    assert "be specific about X" in critique
    assert "0.10" in critique


async def test_critique_falls_back_gracefully_on_malformed_or_failed_call() -> None:
    """A critique call that fails outright, or returns no usable `critique_response` tool
    call, must fall back to a generic (but still real) revision note rather than raising —
    an advisor-call failure must never block a bounded retry (mirrors every other "never
    raise past this layer" contract in scorecard_builder.py)."""
    fake_bedrock_raises = FakeBedrockClient(
        converse_fn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated Bedrock outage"))
    )
    critique = await sb._generate_quality_gate_critique(
        fake_bedrock_raises, None, task_context="Do the task.", produced_output="An answer.", gate_score=0.2
    )
    assert "0.20" in critique
    assert "improved" in critique.lower()

    fake_bedrock_malformed = FakeBedrockClient(converse_fn=lambda **kwargs: text_result("not a tool call"))
    critique2 = await sb._generate_quality_gate_critique(
        fake_bedrock_malformed, None, task_context="Do the task.", produced_output="An answer.", gate_score=0.3
    )
    assert "0.30" in critique2


async def test_critique_second_failure_includes_previous_critique_and_prior_output() -> None:
    """On the SECOND failure of the same task, the critique prompt must carry what the
    first critique suggested and what the response looked like BEFORE that critique was
    applied, so the advisor can see what changed and refine instead of repeating itself."""
    captured_system: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        captured_system.append(system or "")
        return tool_use_result(
            "critique_response",
            {"specific_problems": ["still missing Y"], "concrete_fix": "add Y explicitly"},
        )

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    await sb._generate_quality_gate_critique(
        fake_bedrock,
        None,
        task_context="Do the task.",
        produced_output="Second attempt — still no Y.",
        gate_score=0.4,
        previous_critique="FIRST_CRITIQUE_MARKER: add X.",
        previous_output="First attempt — no X, no Y.",
    )

    assert len(captured_system) == 1
    prompt = captured_system[0]
    assert "SECOND FAILURE" in prompt
    assert "FIRST_CRITIQUE_MARKER" in prompt
    assert "First attempt — no X, no Y." in prompt
    assert "Second attempt — still no Y." in prompt


async def test_checkpoint3_retry_sees_previous_response_and_critique() -> None:
    """The retried decision must see the response being critiqued (not only the critique)."""
    session_id = str(uuid.uuid4())
    propose_messages: list[str] = []
    scores = iter([0.2, 0.9])

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        if "critique_response" in _tools_offered(tools):
            return tool_use_result(
                "critique_response", {"specific_problems": ["CRITIQUE_MARKER: off-topic."], "concrete_fix": "Fix it."}
            )
        propose_messages.append(str(messages))
        return tool_use_result(
            "ask_clarification",
            {"question": f"ANSWER_MARKER_{len(propose_messages)}", "options": [], "missing_fields": ["name"]},
        )

    fake_jev = FakeJevClient(rate_fn=lambda *, instruction, answer: next(scores))
    await sb.start_session(
        session_id, "Build me a scorecard for X.", FakeBedrockClient(converse_fn=converse_fn), jev_client=fake_jev
    )

    assert len(propose_messages) == 2
    assert "ANSWER_MARKER_1" in propose_messages[1]
    assert "CRITIQUE_MARKER" in propose_messages[1]
