"""Tests for the multi-agent, CATEGORY-based research fan-out (`research_kpis` node + the
single `_run_research_agent` worker it invokes once PER CATEGORY, concurrently — see
app/ai/scorecard_builder.py). Uses `FakeBedrockClient`/`FakeWebSearchClient` (see
tests/fakes.py); no real network calls (the real Gateway/Bedrock are only exercised by the
live end-to-end pass, not by `pytest`).

Covers exactly what the task calls out:
- CATEGORIES (not generic "research angles") are decided and ONE research agent is
  dispatched per category, genuinely CONCURRENTLY, not sequentially
  (`test_research_agents_run_concurrently_not_sequentially`, via a `threading.Barrier` —
  see that test's docstring for why a barrier is a more reliable proof than wall-clock
  timing here);
- each agent's proposed KPIs land under ITS OWN category (`parent_name` set correctly,
  `level=2`), and the category itself becomes a real `level=1` KpiDraft
  (`test_kpi_batches_merge_from_multiple_categories`);
- cross-category dedup actually removes a near-duplicate KPI concept that was deliberately
  scripted to appear under two DIFFERENT categories (same test — "Response Time" under
  category 1 vs. "Response Time Speed" under category 2);
- a failing individual research agent doesn't crash the whole turn
  (`test_one_failing_research_agent_does_not_crash_the_turn`);
- the final KPI proposal incorporates findings from more than one category, not just the
  first one that returns (`test_final_proposal_grounded_in_multiple_categories`);
- the existing iterative-research-round mechanism still works correctly with categories,
  including a round 2 that adds a genuinely NEW category (bottom of this file, under
  "Iterative / multi-round research").

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
mechanism itself (a scripted "not confident" response genuinely triggering round 2 with a
NEW category and more KPIs, and the round cap actually stopping an always-insufficient
script) is covered by its own tests at the bottom of this file, under "Iterative / multi-round
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
    {"sufficient": True, "reasoning": "Coverage looks adequate for this domain.", "next_categories": []},
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

# Real, business-recognizable category names (not abstract "research angle" labels), per
# the product owner's own framing of this feature.
_CATEGORIES = [
    {"name": "Security Standards", "focus": "ISO 27001 / NIST vendor security review controls"},
    {"name": "Regulatory Compliance", "focus": "SOC 2 vendor compliance review requirements"},
    {"name": "Vendor Risk Practice", "focus": "vendor risk assessment scoring frameworks"},
]


def _tools_offered(tools) -> set[str]:
    return {t.name for t in (tools or [])}


def _category_from_system(system: str | None) -> str | None:
    """The `propose_kpi_batch` call's category identity lives in its `system` prompt (see
    `_PROPOSE_KPI_BATCH_SYSTEM_PROMPT_TEMPLATE`: 'investigated category "{category}"'), not
    in `messages` (unlike the research-worker's own first call — see
    `_category_from_messages`), since that call's own `local_messages` is a fresh one-shot
    list with no category text."""
    if not system:
        return None
    marker = 'investigated category "'
    idx = system.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    end = system.find('"', start)
    return system[start:end] if end != -1 else None


def _category_from_messages(messages) -> str | None:
    """Research-worker calls carry `Category: {category}` in their first user message (see
    `_run_research_agent`) — used here to identify which category a given concurrent call
    belongs to, since `asyncio.gather` gives no ordering guarantee."""
    for m in messages:
        for block in m.get("content", []):
            text = block.get("text", "")
            if text.startswith("Category: "):
                return text.split("Category: ", 1)[1].split("\n", 1)[0]
    return None


def _kpis_by_level(kpis: list[dict]) -> tuple[list[dict], list[dict]]:
    categories = [k for k in kpis if k["level"] == 1]
    children = [k for k in kpis if k["level"] == 2]
    return categories, children


# --- Concurrency proof -------------------------------------------------------------------


async def test_research_agents_run_concurrently_not_sequentially() -> None:
    """Proves the N per-category research-agent calls are genuinely in flight together, not
    one after another.

    Uses a `threading.Barrier(len(_CATEGORIES))`: each per-category worker's very first
    `.converse()` call blocks on `barrier.wait()` before returning its scripted response.
    `.converse()` runs inside `asyncio.to_thread` (real OS threads), so:
    - if the agents are dispatched CONCURRENTLY (via `asyncio.gather`, as implemented),
      all `len(_CATEGORIES)` threads reach the barrier at roughly the same time and all
      proceed — the call below completes normally.
    - if they were (bugfully) dispatched SEQUENTIALLY instead, the first agent's thread
      would block forever at the barrier waiting for the other agents' threads, which
      would never even start (the second agent's `_run_research_agent` coroutine would
      not begin until the first `await`s all the way through) — the barrier times out and
      raises `BrokenBarrierError`, which this test lets propagate as a hard failure rather
      than a flaky timing assertion.
    """
    session_id = str(uuid.uuid4())
    barrier = threading.Barrier(len(_CATEGORIES), timeout=5)

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            # Every per-category worker's first call lands here — synchronize on the
            # barrier BEFORE returning, proving all of them were genuinely concurrently
            # in flight.
            barrier.wait()
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {category}",
                    "suggested_kpis": [],
                    "suggested_thresholds": [],
                    "sources": [],
                },
            )

        if "propose_kpi_batch" in names:
            # Each agent's own KPI-batch proposal — not this test's focus, so an empty
            # batch is fine here.
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
    # synchronized. Only if all `len(_CATEGORIES)` worker threads were GENUINELY
    # concurrently in flight does `barrier.wait()` return normally for all of them (each
    # returns its real "Finding for {category}" via record_research_finding); if they ran
    # sequentially, every worker but the last would block until the 5s timeout, raise
    # `BrokenBarrierError`, and fall back to a degraded (summary-less) finding instead —
    # which the assertions below would catch.
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    assert len(findings) == len(_CATEGORIES)
    summaries = {f["category"]: f["summary"] for f in findings}
    for c in _CATEGORIES:
        assert summaries.get(c["name"]) == f"Finding for {c['name']}", (
            f"category {c['name']!r} did not complete normally through the barrier — "
            "the research agents were not genuinely concurrent."
        )


# --- Graceful degradation ------------------------------------------------------------------


async def test_one_failing_research_agent_does_not_crash_the_turn() -> None:
    """One category's research agent raises on every call it makes (simulating a Bedrock/
    web_search failure specific to that agent); the other categories still succeed, and the
    overall chat turn completes normally — proving a single failing agent is excluded from
    consolidation rather than poisoning the whole turn."""
    session_id = str(uuid.uuid4())
    failing_category = _CATEGORIES[1]["name"]

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            if category == failing_category:
                raise RuntimeError("simulated total failure for this research agent")
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {category}",
                    "suggested_kpis": [{"name": f"KPI from {category}", "rationale": "grounded"}],
                    "suggested_thresholds": [],
                    "sources": [{"title": f"Source for {category}", "url": "https://example.com"}],
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
    categories_present = {f["category"] for f in findings}
    # The two succeeding categories are present and usable...
    assert _CATEGORIES[0]["name"] in categories_present
    assert _CATEGORIES[2]["name"] in categories_present
    # ...and the failing one was cleanly excluded (degraded/empty), not poisoning the rest.
    assert failing_category not in categories_present


# --- Multi-category grounding -----------------------------------------------------------------


async def test_final_proposal_grounded_in_multiple_categories() -> None:
    """The consolidated research context handed to the model's final update_draft turn
    contains distinguishing content from MORE THAN ONE category — not just the first agent
    that happened to return — proving real consolidation, not a "first result wins" bug."""
    session_id = str(uuid.uuid4())
    final_system_prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"UNIQUE_SUMMARY_MARKER[{category}]",
                    "suggested_kpis": [{"name": f"KPI[{category}]", "rationale": "r"}],
                    "suggested_thresholds": [
                        {"metric": f"metric[{category}]", "value_or_range": "X", "source_note": "n"}
                    ],
                    "sources": [{"title": f"SOURCE[{category}]", "url": "https://example.com/" + category}],
                },
            )

        if "propose_kpi_batch" in names:
            # Not this test's focus (it's about research_context grounding, not KPI
            # batching itself — see test_kpi_batches_merge_from_multiple_categories below
            # for that) — an empty batch keeps this test isolated to its original concern.
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

    # Grounding from at least TWO distinct categories must be present in what the model saw
    # when it made its actual KPI proposal — not just the first category to finish.
    markers_present = [f"UNIQUE_SUMMARY_MARKER[{c['name']}]" in prompt for c in _CATEGORIES]
    assert sum(markers_present) >= 2, f"expected >=2 categories grounded in final prompt, got {markers_present}"


# --- Variable category count / narrow-domain fallback ---------------------------------------


async def test_zero_categories_falls_back_to_plain_propose_kpis() -> None:
    """When the model decides a domain needs no dedicated category structure/research
    fan-out (categories=[]), research_kpis is a clean no-op and propose_kpis proceeds
    exactly as it did before this feature existed — proving the master step doesn't force
    research/categorization where it isn't warranted (narrow/simple domain case)."""
    session_id = str(uuid.uuid4())
    calls: list[set[str]] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        calls.append(_tools_offered(tools))
        if "decide_categories" in calls[-1]:
            return tool_use_result("decide_categories", {"categories": []})
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
    assert len(calls) == 2  # exactly: decide_categories, then the one real propose_kpis turn

    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    assert snapshot.values.get("research_findings") is None
    assert snapshot.values.get("research_done") is True


# --- Total fan-out failure -> graceful degradation to model's own knowledge ----------------


def _guideline_rungs(base_text: str) -> dict[str, dict]:
    return {str(lvl): {"qualitative_text": f"{base_text} — level {lvl}."} for lvl in range(11)}


async def test_kpi_batches_merge_from_multiple_categories() -> None:
    """THE category-restructure fix, end to end: each research agent proposes its OWN small
    batch of fully-specified KPIs for its SINGLE assigned category via `propose_kpi_batch`,
    and `research_kpis` merges every agent's batch into `draft.kpis` BEFORE `propose_kpis`'s
    first real turn ever runs — proving:
    - the final KPI count grows with the number of research agents/batches (here: 3
      categories x 2 KPIs = 6 proposed) rather than being capped at what one single
      end-of-pipeline tool call could produce;
    - every surviving child KPI's `parent_name` is set to EXACTLY the category it was
      researched under (`level=2`), and each category itself materializes as a real
      `level=1` KpiDraft with `parent_name=None`;
    - the dedup pass collapses a genuine near-duplicate concept proposed by two DIFFERENT
      categories ("Response Time" under Security Standards / "Response Time Speed" under
      Regulatory Compliance) — proving CROSS-category dedup, not just within one category;
    - weights are renormalized to sum to 100 WITHIN each surviving category, and the
      category-level weights themselves also sum to 100 across categories."""
    session_id = str(uuid.uuid4())

    per_category_kpis = {
        _CATEGORIES[0]["name"]: ["Response Time", "Control Coverage"],
        _CATEGORIES[1]["name"]: ["Response Time Speed", "Audit Trail Completeness"],  # first is a near-dup
        _CATEGORIES[2]["name"]: ["Risk Scoring Accuracy", "Escalation Clarity"],
    }

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})

        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE

        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": f"Finding for {category}",
                    "suggested_kpis": [],
                    "suggested_thresholds": [],
                    "sources": [],
                },
            )

        if "propose_kpi_batch" in names:
            category = _category_from_system(system)
            kpi_names = per_category_kpis[category]
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
    all_kpis = turn.draft["kpis"]
    category_nodes, children = _kpis_by_level(all_kpis)

    # 3 categories x 2 KPIs = 6 proposed, minus 1 deduped cross-category near-duplicate = 5
    # distinct children — MEANINGFULLY more than what a single agent's own batch (2) or the
    # old hardcoded "4" ceiling would produce, and driven entirely by what the (fake)
    # research surfaced, not a fixed target count.
    assert len(children) == 5, [k["name"] for k in children]
    # More than any SINGLE category's own batch could produce alone (each category here
    # proposed only 2) — the total genuinely grew with the number of categories researched.
    assert len(children) > 2
    child_names = [k["name"] for k in children]
    assert "Response Time" in child_names
    assert "Response Time Speed" not in child_names  # collapsed by the cross-category dedup pass
    for expected in ["Control Coverage", "Audit Trail Completeness", "Risk Scoring Accuracy", "Escalation Clarity"]:
        assert expected in child_names

    # Every surviving category materialized as its own level=1 KpiDraft (parent_name=None).
    assert {c["name"] for c in category_nodes} == {c["name"] for c in _CATEGORIES}
    assert all(c["parent_name"] is None for c in category_nodes)
    assert all(c["level"] == 1 for c in category_nodes)
    # Category nodes carry no guidelines of their own (only leaf/child KPIs are judged —
    # see app/ai/judge.py::leaf_nodes).
    assert all(c["guidelines"] == {} for c in category_nodes)

    # Every child KPI's parent_name points at EXACTLY its own researched category, never a
    # sibling's or an unrelated one.
    by_child_name = {k["name"]: k for k in children}
    assert by_child_name["Response Time"]["parent_name"] == _CATEGORIES[0]["name"]
    assert by_child_name["Control Coverage"]["parent_name"] == _CATEGORIES[0]["name"]
    assert by_child_name["Audit Trail Completeness"]["parent_name"] == _CATEGORIES[1]["name"]
    assert by_child_name["Risk Scoring Accuracy"]["parent_name"] == _CATEGORIES[2]["name"]
    assert by_child_name["Escalation Clarity"]["parent_name"] == _CATEGORIES[2]["name"]
    assert all(k["level"] == 2 for k in children)

    # Every merged CHILD KPI kept its full 11-level guidelines from its own agent's batch
    # call — never re-derived by a separate downstream call. Category (level=1) nodes are
    # correctly exempt (see assertion above).
    for kpi in children:
        assert len(kpi["guidelines"]) == 11

    # Category-level weights sum to 100 across the 3 surviving categories...
    category_total = sum(c["weight"] for c in category_nodes)
    assert category_total == pytest.approx(100.0, abs=0.05)
    # ...and each category's own children separately sum to 100 WITHIN that category (each
    # agent weighted its own batch to ~100 independently; the merge step rescales each
    # category's surviving pool back down to sum to 100).
    for category in _CATEGORIES:
        group = [k for k in children if k["parent_name"] == category["name"]]
        assert sum(k["weight"] for k in group) == pytest.approx(100.0, abs=0.05), category["name"]


async def test_category_decision_failure_falls_back_gracefully_without_crashing_turn() -> None:
    """If deciding categories itself fails (simulating Bedrock being unavailable for that
    specific call), research_kpis must not crash the turn — propose_kpis still proceeds and
    proposes KPIs from its own knowledge (+ its own ad hoc web_search fallback), exactly the
    "propose_kpis should still be able to fall back... without crashing the turn" contract."""
    session_id = str(uuid.uuid4())

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            raise RuntimeError("simulated Bedrock unavailability for category decision")
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
# confident" response genuinely triggers a second round with a NEW category and MORE KPIs
# merged in; (2) the round cap (MAX_RESEARCH_ROUNDS) is actually enforced — a client
# scripted to ALWAYS say "not confident" still stops at the cap, not an infinite loop; (3) a
# normal "confident after round 1" path still works with no regression (single round, same
# as before this feature existed). Also proves categories and rounds COMPOSE correctly: a
# round-2 category merges in as its own additional Level-1 node alongside round 1's.

_ROUND_2_CATEGORY = {
    "name": "Audit Cadence",
    "focus": "recommended vendor security audit frequency benchmarks",
}


def _guideline_rungs_research(base_text: str) -> dict[str, dict]:
    return {str(lvl): {"qualitative_text": f"{base_text} — level {lvl}."} for lvl in range(11)}


async def test_low_confidence_triggers_second_round_with_new_category() -> None:
    """The confidence check (`assess_research_coverage`) reporting `sufficient: False` with
    a genuinely NEW category after round 1 causes a real second round: round 2's research
    agent actually runs (against the NEW category, never a repeat of a round-1 category),
    its own KPI batch is merged in on top of round 1's AS ITS OWN ADDITIONAL CATEGORY, and
    the consolidated findings include both rounds — proving this is a real second fan-out,
    not just a re-decided round 1, and that categories/rounds compose correctly."""
    session_id = str(uuid.uuid4())
    assess_calls: list[dict] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})

        if "assess_research_coverage" in names:
            assess_calls.append({"system": system})
            return tool_use_result(
                "assess_research_coverage",
                {
                    "sufficient": False,
                    "reasoning": "Missing coverage on third-party audit cadence expectations.",
                    "next_categories": [_ROUND_2_CATEGORY],
                },
            )

        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {"summary": f"Finding for {category}", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )

        if "propose_kpi_batch" in names:
            category = _category_from_system(system)
            kpi_name = f"KPI for {category}"
            return tool_use_result(
                "propose_kpi_batch",
                {"kpis": [{"name": kpi_name, "weight": 100, "guidelines": _guideline_rungs_research(kpi_name)}]},
            )

        # propose_kpis's real (only) turn: fill in the remaining top-level scalar fields
        # ONLY, deliberately OMITTING "kpis" from the patch — so update_draft's merge
        # leaves the already-merged (both-rounds) KPI set from research_kpis untouched,
        # proving it landed in draft state BEFORE this call ever ran (mirrors
        # test_kpi_batches_merge_from_multiple_categories's own pattern for the same
        # reason).
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

    all_kpis = turn.draft["kpis"]
    category_nodes, children = _kpis_by_level(all_kpis)
    all_categories = [*_CATEGORIES, _ROUND_2_CATEGORY]

    # 4 categories total (3 from round 1 + 1 new one from round 2), each with its own
    # level=1 node, and each with exactly 1 child (the one KPI its agent proposed) —
    # proving round 2's category composed in as a genuinely ADDITIONAL category, not
    # replacing or merging into an existing one.
    assert {c["name"] for c in category_nodes} == {c["name"] for c in all_categories}
    assert len(children) == len(all_categories)
    child_names = [k["name"] for k in children]
    for category in all_categories:
        expected_child = f"KPI for {category['name']}"
        assert expected_child in child_names, (expected_child, child_names)
        child = next(k for k in children if k["name"] == expected_child)
        assert child["parent_name"] == category["name"]

    # Round-2's category genuinely ran its own agent and is traceable in the consolidated
    # findings — both rounds' categories are present, not just round 1's.
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    findings = snapshot.values.get("research_findings") or []
    categories_present = {f["category"] for f in findings}
    for category in all_categories:
        assert category["name"] in categories_present, categories_present


async def test_round_cap_enforced_even_if_always_low_confidence() -> None:
    """A client scripted to ALWAYS report `sufficient: False` (with a fresh, never-repeated
    category every time it's asked) still stops at MAX_RESEARCH_ROUNDS — proving the cap is
    a genuine hard stop, not a suggestion the model could talk its way past into an
    unbounded (or effectively infinite) loop."""
    session_id = str(uuid.uuid4())
    assess_call_count = [0]

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = _tools_offered(tools)
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})

        if "assess_research_coverage" in names:
            assess_call_count[0] += 1
            n = assess_call_count[0]
            return tool_use_result(
                "assess_research_coverage",
                {
                    "sufficient": False,
                    "reasoning": f"Still missing something (assessment #{n}) — never satisfied.",
                    "next_categories": [{"name": f"Extra Category {n}", "focus": f"focus {n}"}],
                },
            )

        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            return tool_use_result(
                "record_research_finding",
                {"summary": f"Finding for {category}", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
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
    categories_present = {f["category"] for f in findings}
    # Exactly round 1's categories (3) + round 2's one new category ("Extra Category 1") —
    # never a round 3's "Extra Category 2", which would only exist if the cap failed to
    # stop the loop.
    assert categories_present == {*(c["name"] for c in _CATEGORIES), "Extra Category 1"}
    assert "Extra Category 2" not in categories_present


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
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": _CATEGORIES})
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            category = _category_from_messages(messages)
            record_calls.append(category)
            return tool_use_result(
                "record_research_finding",
                {"summary": f"Finding for {category}", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
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
    # record_research_finding was called exactly once per round-1 category, never again for
    # a "round 2" — the confidence check genuinely stopped the loop after one round.
    assert sorted(record_calls) == sorted(c["name"] for c in _CATEGORIES)
