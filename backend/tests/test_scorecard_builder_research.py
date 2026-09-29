"""Tests for the multi-agent research fan-out (`research_kpis` node + the single
`_run_research_agent` worker it invokes N times concurrently — see
app/ai/scorecard_builder.py). Uses `FakeBedrockClient`/`FakeWebSearchClient` (see
tests/fakes.py); no real network calls (the real Gateway/Bedrock are only exercised by the
live end-to-end pass, not by `pytest`).

Covers exactly what the task calls out:
- multiple research angles are genuinely dispatched CONCURRENTLY, not sequentially
  (`test_research_agents_run_concurrently_not_sequentially`, via a `threading.Barrier` —
  see that test's docstring for why a barrier is a more reliable proof than wall-clock
  timing here);
- a failing individual research agent doesn't crash the whole turn
  (`test_one_failing_research_agent_does_not_crash_the_turn`);
- the final KPI proposal incorporates findings from more than one angle, not just the
  first one that returns (`test_final_proposal_grounded_in_multiple_angles`).

`FakeBedrockClient.converse`'s `converse_fn` mode is invoked OUTSIDE its bookkeeping lock
(see tests/fakes.py) specifically so these tests can prove genuine thread-level concurrency
across the `asyncio.to_thread`-dispatched `.converse()` calls research agents make.

**Iterative / multi-round research**: every `converse_fn` below now explicitly scripts
`assess_research_coverage` (the confidence check `research_kpis` runs after round 1 — see
`_assess_research_coverage`/`MAX_RESEARCH_ROUNDS` in `scorecard_builder.py`) to return
`sufficient: True` — these tests are all deliberately exercising the SINGLE-round golden
path (proving no regression from the multi-round feature), so they answer "yes, coverage
is sufficient" immediately rather than letting the call fall through to a test's catch-all
branch (which would return a mismatched tool response — that's still handled safely by
`_assess_research_coverage`'s own fail-safe, but explicitly scripting it here keeps each
test's own call-count/content assertions exact and intentional). The dedicated multi-round
mechanism itself (a scripted "not confident" response genuinely triggering round 2 with new
angles/more KPIs, and the round cap actually stopping an always-insufficient script) is
covered by its own tests at the bottom of this file, under "Iterative / multi-round
research".
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.ai import scorecard_builder as sb
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")

_SUFFICIENT_COVERAGE = tool_use_result(
    "assess_research_coverage",
    {"sufficient": True, "reasoning": "Coverage looks adequate for this domain.", "next_angles": []},
)

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

_ANGLES = [
    {"angle": "Security standards & frameworks", "query_focus": "ISO 27001 / NIST vendor security review controls"},
    {"angle": "Regulatory/compliance requirements", "query_focus": "SOC 2 vendor compliance review requirements"},
    {"angle": "Vendor risk assessment practice", "query_focus": "vendor risk assessment scoring frameworks"},
]


def _tools_offered(tools) -> set[str]:
    return {t.name for t in (tools or [])}


def _angle_from_system(system: str | None) -> str | None:
    """The `propose_kpi_batch` call's angle identity lives in its `system` prompt (see
    `_PROPOSE_KPI_BATCH_SYSTEM_PROMPT_TEMPLATE`: 'investigated angle "{angle}"'), not in
    `messages` (unlike the research-worker's own first call — see `_angle_from_messages`),
    since that call's own `local_messages` is a fresh one-shot list with no angle text."""
    if not system:
        return None
    marker = 'investigated angle "'
    idx = system.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    end = system.find('"', start)
    return system[start:end] if end != -1 else None


def _angle_from_messages(messages) -> str | None:
    """Research-worker calls carry `Research angle: {angle}` in their first user message
    (see `_run_research_agent`) — used here to identify which angle a given concurrent
    call belongs to, since `asyncio.gather` gives no ordering guarantee."""
    for m in messages:
        for block in m.get("content", []):
            text = block.get("text", "")
            if text.startswith("Research angle: "):
                return text.split("Research angle: ", 1)[1].split("\n", 1)[0]
    return None


# --- Concurrency proof -------------------------------------------------------------------


async def test_research_agents_run_concurrently_not_sequentially() -> None:
    """Proves the N per-angle research-agent calls are genuinely in flight together, not
    one after another.

    Uses a `threading.Barrier(len(_ANGLES))`: each per-angle worker's very first
    `.converse()` call blocks on `barrier.wait()` before returning its scripted response.
    `.converse()` runs inside `asyncio.to_thread` (real OS threads), so:
    - if the agents are dispatched CONCURRENTLY (via `asyncio.gather`, as implemented),
      all `len(_ANGLES)` threads reach the barrier at roughly the same time and all
      proceed — the call below completes normally.
    - if they were (bugfully) dispatched SEQUENTIALLY instead, the first agent's thread
      would block forever at the barrier waiting for the other agents' threads, which
      would never even start (the second agent's `_run_research_agent` coroutine would
      not begin until the first `await`s all the way through) — the barrier times out and
      raises `BrokenBarrierError`, which this test lets propagate as a hard failure rather
      than a flaky timing assertion.
    """
    session_id = str(uuid.uuid4())
    barrier = threading.Barrier(len(_ANGLES), timeout=5)

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            # Every per-angle worker's first call lands here — synchronize on the barrier
            # BEFORE returning, proving all of them were genuinely concurrently in flight.
            barrier.wait()
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {angle}",
                    "suggested_kpis": [],
                    "suggested_thresholds": [],
                    "sources": [],
                },
            )

        if "propose_kpi_batch" in names:
            # Each agent's own KPI-batch proposal (Part 1's fix — see
            # app/ai/scorecard_builder.py's module comment above MAX_RESEARCH_ANGLES).
            # Not this test's focus, so an empty batch is fine here.
            return tool_use_result("propose_kpi_batch", {"kpis": []})

        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"

    # Crucial: assert on the CONTENT of the consolidated findings, not merely that N
    # `.converse()` calls were recorded — `FakeBedrockClient` logs a call to `self.calls`
    # the instant it's dispatched, *before* invoking `converse_fn`/hitting the barrier, so
    # a call-count alone would be recorded identically whether or not the barrier actually
    # synchronized. Only if all `len(_ANGLES)` worker threads were GENUINELY concurrently
    # in flight does `barrier.wait()` return normally for all of them (each returns its
    # real "Finding for {angle}" via record_research_finding); if they ran sequentially,
    # every worker but the last would block until the 5s timeout, raise
    # `BrokenBarrierError`, and fall back to a degraded (summary-less) finding instead —
    # which the assertions below would catch.
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    assert len(findings) == len(_ANGLES)
    summaries = {f["angle"]: f["summary"] for f in findings}
    for a in _ANGLES:
        assert summaries.get(a["angle"]) == f"Finding for {a['angle']}", (
            f"angle {a['angle']!r} did not complete normally through the barrier — "
            "the research agents were not genuinely concurrent."
        )


# --- Graceful degradation ------------------------------------------------------------------


async def test_one_failing_research_agent_does_not_crash_the_turn() -> None:
    """One angle's research agent raises on every call it makes (simulating a Bedrock/
    web_search failure specific to that agent); the other angles still succeed, and the
    overall chat turn completes normally — proving a single failing agent is excluded
    from consolidation rather than poisoning the whole turn."""
    session_id = str(uuid.uuid4())
    failing_angle = _ANGLES[1]["angle"]

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            if angle == failing_angle:
                raise RuntimeError("simulated total failure for this research agent")
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {angle}",
                    "suggested_kpis": [{"name": f"KPI from {angle}", "rationale": "grounded"}],
                    "suggested_thresholds": [],
                    "sources": [{"title": f"Source for {angle}", "url": "https://example.com"}],
                },
            )

        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})

        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    # The turn completed successfully despite one research agent raising on every call.
    assert turn.status == "confirmed"

    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    angles_present = {f["angle"] for f in findings}
    # The two succeeding angles are present and usable...
    assert _ANGLES[0]["angle"] in angles_present
    assert _ANGLES[2]["angle"] in angles_present
    # ...and the failing one was cleanly excluded (degraded/empty), not poisoning the rest.
    assert failing_angle not in angles_present


# --- Multi-angle grounding -----------------------------------------------------------------


async def test_final_proposal_grounded_in_multiple_angles() -> None:
    """The consolidated research context handed to the model's final update_draft turn
    contains distinguishing content from MORE THAN ONE angle — not just the first agent
    that happened to return — proving real consolidation, not a "first result wins" bug."""
    session_id = str(uuid.uuid4())
    final_system_prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"UNIQUE_SUMMARY_MARKER[{angle}]",
                    "suggested_kpis": [{"name": f"KPI[{angle}]", "rationale": "r"}],
                    "suggested_thresholds": [
                        {"metric": f"metric[{angle}]", "value_or_range": "X", "source_note": "n"}
                    ],
                    "sources": [{"title": f"SOURCE[{angle}]", "url": "https://example.com/" + angle}],
                },
            )

        if "propose_kpi_batch" in names:
            # Not this test's focus (it's about research_context grounding, not KPI
            # batching itself — see test_kpi_batches_merge_from_multiple_agents below for
            # that) — an empty batch keeps this test isolated to its original concern.
            return tool_use_result("propose_kpi_batch", {"kpis": []})

        # This is propose_kpis's real turn — capture its system prompt for inspection.
        final_system_prompts.append(system or "")
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"
    assert len(final_system_prompts) == 1
    prompt = final_system_prompts[0]

    # Grounding from at least TWO distinct angles must be present in what the model saw
    # when it made its actual KPI proposal — not just the first angle to finish.
    markers_present = [f"UNIQUE_SUMMARY_MARKER[{a['angle']}]" in prompt for a in _ANGLES]
    assert sum(markers_present) >= 2, f"expected >=2 angles grounded in final prompt, got {markers_present}"


# --- Variable angle count / narrow-domain fallback -----------------------------------------


async def test_zero_angles_falls_back_to_plain_propose_kpis() -> None:
    """When the model decides a domain needs no dedicated research fan-out (angles=[]),
    research_kpis is a clean no-op and propose_kpis proceeds exactly as it did before this
    feature existed — proving the master step doesn't force research where it isn't
    warranted (narrow/simple domain case)."""
    session_id = str(uuid.uuid4())
    calls: list[set[str]] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        calls.append(_tools_offered(tools))
        if "decide_research_angles" in calls[-1]:
            return tool_use_result("decide_research_angles", {"angles": []})
        return tool_use_result(
            "update_draft",
            {
                "patch": {
                    "name": "Simple Checklist",
                    "purpose": "Rate a simple checklist.",
                    "domain": "Internal ops",
                    "audience": "Ops team",
                    "target_score": 8,
                    "kpis": [
                        {
                            "name": "Completeness",
                            "weight": 100,
                            "level": 1,
                            "guidelines": {
                                "10": {"qualitative_text": "All items done."},
                                "0": {"qualitative_text": "Nothing done."},
                            },
                        }
                    ],
                },
                "confirmed": True,
            },
        )

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id, "Build me a simple internal checklist scorecard.", fake_bedrock, web_search_client=fake_search
    )

    assert turn.status == "confirmed"
    assert len(calls) == 2  # exactly: decide_research_angles, then the one real propose_kpis turn

    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    assert snapshot.values.get("research_findings") is None
    assert snapshot.values.get("research_done") is True


# --- Total fan-out failure -> graceful degradation to model's own knowledge ----------------


def _guideline_rungs(base_text: str) -> dict[str, dict]:
    return {str(lvl): {"qualitative_text": f"{base_text} — level {lvl}."} for lvl in range(11)}


async def test_kpi_batches_merge_from_multiple_agents() -> None:
    """THE Part 1 fix, end to end: each research agent proposes its OWN small batch of
    fully-specified KPIs (grounded in its own research angle) via `propose_kpi_batch`, and
    `research_kpis` merges every agent's batch into `draft.kpis` BEFORE `propose_kpis`'s
    first real turn ever runs — proving the final KPI count grows with the number of
    research agents/batches (here: 3 agents x 2 KPIs = 6 proposed) rather than being capped
    at what one single end-of-pipeline tool call could produce, AND that the dedup pass
    collapses a genuine near-duplicate concept proposed by two different agents ("Response
    Time" / "Response Time Speed"), AND that weights are renormalized to sum to 100."""
    session_id = str(uuid.uuid4())

    per_angle_kpis = {
        _ANGLES[0]["angle"]: ["Response Time", "Control Coverage"],
        _ANGLES[1]["angle"]: ["Response Time Speed", "Audit Trail Completeness"],  # first is a near-dup
        _ANGLES[2]["angle"]: ["Risk Scoring Accuracy", "Escalation Clarity"],
    }

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {angle}",
                    "suggested_kpis": [],
                    "suggested_thresholds": [],
                    "sources": [],
                },
            )

        if "propose_kpi_batch" in names:
            angle = _angle_from_system(system)
            kpi_names = per_angle_kpis[angle]
            kpis = [
                {"name": n, "weight": 50, "guidelines": _guideline_rungs(n)}
                for n in kpi_names
            ]
            return tool_use_result("propose_kpi_batch", {"kpis": kpis})

        # propose_kpis's real (only) turn: fill in the remaining top-level scalar fields
        # ONLY — deliberately omitting "kpis" from the patch, so update_draft's merge
        # (`{**current, **patch}`) leaves the already-merged KPI set from research_kpis
        # completely untouched, proving it landed in draft state BEFORE this call ever ran
        # rather than being (re)generated by it.
        return tool_use_result(
            "update_draft",
            {
                "patch": {
                    "name": "Vendor Security Compliance Review Quality",
                    "purpose": "Rate the quality of vendor security compliance reviews.",
                    "domain": "Vendor Risk Management",
                    "audience": "Procurement/security leads",
                    "target_score": 8,
                },
                "confirmed": True,
            },
        )

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"
    kpi_names_final = [k["name"] for k in turn.draft["kpis"]]

    # 3 agents x 2 KPIs = 6 proposed, minus 1 deduped near-duplicate = 5 distinct KPIs —
    # MEANINGFULLY more than what a single agent's own batch (2) or the old hardcoded "4"
    # ceiling would produce, and driven entirely by what the (fake) research surfaced, not
    # a fixed target count.
    assert len(kpi_names_final) == 5, kpi_names_final
    assert len(kpi_names_final) > sb.MAX_KPIS_PER_RESEARCH_BATCH
    assert "Response Time" in kpi_names_final
    assert "Response Time Speed" not in kpi_names_final  # collapsed by the dedup pass
    for expected in ["Control Coverage", "Audit Trail Completeness", "Risk Scoring Accuracy", "Escalation Clarity"]:
        assert expected in kpi_names_final

    # Every merged KPI kept its full 11-level guidelines from its own agent's batch call —
    # never re-derived by a separate downstream call.
    for kpi in turn.draft["kpis"]:
        assert len(kpi["guidelines"]) == 11

    # Weights were renormalized (each agent weighted its own batch to ~100 independently;
    # the merge step rescales the WHOLE pool back down to sum to 100).
    total_weight = sum(k["weight"] for k in turn.draft["kpis"])
    assert total_weight == pytest.approx(100.0, abs=0.05)


async def test_angle_decision_failure_falls_back_gracefully_without_crashing_turn() -> None:
    """If deciding research angles itself fails (simulating Bedrock being unavailable for
    that specific call), research_kpis must not crash the turn — propose_kpis still
    proceeds and proposes KPIs from its own knowledge (+ its own ad hoc web_search
    fallback), exactly the "propose_kpis should still be able to fall back... without
    crashing the turn" contract."""
    session_id = str(uuid.uuid4())

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            raise RuntimeError("simulated Bedrock unavailability for angle decision")
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"


# --- Iterative / multi-round research (end of file) ---------------------------------------
#
# Covers the task's three explicit requirements for this feature: (1) a scripted "not
# confident" response genuinely triggers a second round with NEW angles and MORE KPIs
# merged in; (2) the round cap (MAX_RESEARCH_ROUNDS) is actually enforced — a client
# scripted to ALWAYS say "not confident" still stops at the cap, not an infinite loop; (3) a
# normal "confident after round 1" path still works with no regression (single round, same
# as before this feature existed).

_ROUND_2_ANGLES = [
    {
        "angle": "Third-party audit cadence expectations",
        "query_focus": "recommended vendor security audit frequency benchmarks",
    },
]


def _guideline_rungs_research(base_text: str) -> dict[str, dict]:
    return {str(lvl): {"qualitative_text": f"{base_text} — level {lvl}."} for lvl in range(11)}


async def test_low_confidence_triggers_second_round_with_new_angles() -> None:
    """The confidence check (`assess_research_coverage`) reporting `sufficient: False` with
    genuinely NEW angles after round 1 causes a real second round: round 2's research
    agent(s) actually run (against the NEW angle, never a repeat of a round-1 angle), their
    own KPI batch is merged in on top of round 1's, and the consolidated findings include
    both rounds — proving this is a real second fan-out, not just a re-decided round 1."""
    session_id = str(uuid.uuid4())
    assess_calls: list[dict] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})

        if "assess_research_coverage" in names:
            assess_calls.append({"system": system})
            return tool_use_result(
                "assess_research_coverage",
                {
                    "sufficient": False,
                    "reasoning": "Missing coverage on third-party audit cadence expectations.",
                    "next_angles": _ROUND_2_ANGLES,
                },
            )

        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {"summary": f"Finding for {angle}", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )

        if "propose_kpi_batch" in names:
            angle = _angle_from_system(system)
            kpi_name = f"KPI for {angle}"
            return tool_use_result(
                "propose_kpi_batch",
                {"kpis": [{"name": kpi_name, "weight": 100, "guidelines": _guideline_rungs_research(kpi_name)}]},
            )

        # propose_kpis's real (only) turn: fill in the remaining top-level scalar fields
        # ONLY, deliberately OMITTING "kpis" from the patch — so update_draft's merge
        # leaves the already-merged (both-rounds) KPI set from research_kpis untouched,
        # proving it landed in draft state BEFORE this call ever ran (mirrors
        # test_kpi_batches_merge_from_multiple_agents's own pattern for the same reason).
        return tool_use_result(
            "update_draft",
            {
                "patch": {k: v for k, v in _COMPLETE_PATCH.items() if k != "kpis"},
                "confirmed": True,
            },
        )

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"
    # Exactly one confidence check happened (round 2 == MAX_RESEARCH_ROUNDS, so the cap is
    # reached right after round 2 completes and no third assessment is ever made).
    assert len(assess_calls) == 1

    kpi_names_final = [k["name"] for k in turn.draft["kpis"]]
    round1_kpi_names = [f"KPI for {a['angle']}" for a in _ANGLES]
    round2_kpi_names = [f"KPI for {a['angle']}" for a in _ROUND_2_ANGLES]
    for expected in round1_kpi_names + round2_kpi_names:
        assert expected in kpi_names_final, (expected, kpi_names_final)
    # Genuinely MORE KPIs than round 1 alone would have produced.
    assert len(kpi_names_final) == len(round1_kpi_names) + len(round2_kpi_names)

    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    angles_present = {f["angle"] for f in findings}
    # Both rounds' angles are present in the consolidated findings — round 2 is a REAL
    # additional fan-out, not a no-op or a re-run of round 1's own angles.
    for a in _ANGLES + _ROUND_2_ANGLES:
        assert a["angle"] in angles_present, angles_present


async def test_round_cap_enforced_even_if_always_low_confidence() -> None:
    """A client scripted to ALWAYS report `sufficient: False` (with a fresh, never-repeated
    angle every time it's asked) still stops at MAX_RESEARCH_ROUNDS — proving the cap is a
    genuine hard stop, not a suggestion the model could talk its way past into an unbounded
    (or effectively infinite) loop."""
    session_id = str(uuid.uuid4())
    assess_call_count = [0]

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})

        if "assess_research_coverage" in names:
            assess_call_count[0] += 1
            n = assess_call_count[0]
            return tool_use_result(
                "assess_research_coverage",
                {
                    "sufficient": False,
                    "reasoning": f"Still missing something (assessment #{n}) — never satisfied.",
                    "next_angles": [{"angle": f"Extra angle {n}", "query_focus": f"focus {n}"}],
                },
            )

        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {"summary": f"Finding for {angle}", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )

        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})

        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    # The turn completed at all (no hang) AND used its full budget "confidently" — both are
    # part of proving boundedness. MAX_RESEARCH_ROUNDS=2, so exactly ONE assessment ever
    # runs (after round 1; round 2 hits the cap and skips assessing entirely — see
    # research_kpis's own "reached_cap" short-circuit) even though the script would have
    # happily said "not confident" forever if asked again.
    assert turn.status == "confirmed"
    assert assess_call_count[0] == 1, "expected the cap to stop further assessment calls, not just further rounds"

    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    angles_present = {f["angle"] for f in findings}
    # Exactly round 1's angles (3) + round 2's one new angle ("Extra angle 1") — never a
    # round 3's "Extra angle 2", which would only exist if the cap failed to stop the loop.
    assert angles_present == {*(a["angle"] for a in _ANGLES), "Extra angle 1"}
    assert "Extra angle 2" not in angles_present


async def test_confident_after_round_one_runs_single_round_no_regression() -> None:
    """The ordinary/common case: the confidence check reports `sufficient: True` right
    after round 1 — exactly one round of research agents ever runs, identical to this
    feature's pre-multi-round behavior. Explicit regression coverage for the task's third
    required case, on top of every OTHER test in this file already exercising this same
    single-round path via `_SUFFICIENT_COVERAGE`."""
    session_id = str(uuid.uuid4())
    record_calls: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_research_angles" in names:
            return tool_use_result("decide_research_angles", {"angles": _ANGLES})
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            angle = _angle_from_messages(messages)
            record_calls.append(angle)
            return tool_use_result(
                "record_research_finding",
                {"summary": f"Finding for {angle}", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )
        if "propose_kpi_batch" in names:
            return tool_use_result("propose_kpi_batch", {"kpis": []})
        return tool_use_result("update_draft", {"patch": _COMPLETE_PATCH, "confirmed": True})

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id,
        "I want a scorecard to rate our vendor security compliance reviews.",
        fake_bedrock,
        web_search_client=fake_search,
    )

    assert turn.status == "confirmed"
    # record_research_finding was called exactly once per round-1 angle, never again for a
    # "round 2" — the confidence check genuinely stopped the loop after one round.
    assert sorted(record_calls) == sorted(a["angle"] for a in _ANGLES)
