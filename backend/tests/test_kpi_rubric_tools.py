"""The chat assistant's rubric tools (app/ai/kpi_rubric_tools.py): review (measured Jev confidence + text checks),
per-KPI research, rewriting one KPI / a set / a whole category on the builder's model, and exact level edits. Pure
checks first, then graph-level tests with the assistant's model scripted through `agent_fn`."""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.ai import kpi_rubric_tools as rubric
from app.ai import scorecard_builder as sb
from app.ai.draft_schema import KpiDraft, ScorecardDraft
from app.models.chat_session import ChatSession
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from tests.ai_eval_helpers import seed_scorecard
from tests.fakes import (
    FakeBedrockClient,
    FakeWebSearchClient,
    full_rubric,
    search_result,
    text_result,
    tool_use_result,
)

GOOD_LEVELS = [
    "No delivery milestones are documented anywhere in the submission",
    *[f"{n} of the 10 planned milestones are documented with a dated status update" for n in range(1, 10)],
    "All 10 planned milestones are documented with dated status updates and owners",
]


def _rubric(texts: list[str], criteria: list[dict | None] | None = None) -> dict[str, dict[str, Any]]:
    return {
        str(i): {"qualitative_text": t, "quantitative_criteria": (criteria or [None] * 11)[i]}
        for i, t in enumerate(texts)
    }


def _draft() -> ScorecardDraft:
    """Two categories with two leaves each, plus one top-level leaf."""
    kpis = [
        {"name": "Delivery", "level": 1},
        {"name": "Milestones", "level": 2, "parent_name": "Delivery", "weight": 20, "guidelines": full_rubric("M")},
        {"name": "Status", "level": 2, "parent_name": "Delivery", "weight": 20, "guidelines": full_rubric("S")},
        {"name": "Quality", "level": 1},
        {"name": "Tests", "level": 2, "parent_name": "Quality", "weight": 20, "guidelines": full_rubric("T")},
        {"name": "Docs", "level": 2, "parent_name": "Quality", "weight": 20, "guidelines": full_rubric("D")},
        {"name": "Demo", "level": 1, "weight": 20, "guidelines": full_rubric("X")},
    ]
    return ScorecardDraft.model_validate(
        {"name": "Hackathon", "purpose": "Judge entries", "domain": "Hackathon", "audience": "Judges",
         "target_score": 6, "kpis": kpis}
    )


# --- text checks ------------------------------------------------------------------------------------------------


def test_a_concrete_rubric_has_no_problems() -> None:
    assert rubric.lint_rubric(KpiDraft(name="M", guidelines=_rubric(GOOD_LEVELS))) == []


def test_terse_duplicate_and_circular_rubrics_are_flagged() -> None:
    terse = ["None", "Weak", "Weak", *[f"Level {i}" for i in range(3, 11)]]
    issues = rubric.lint_rubric(KpiDraft(name="K", guidelines=_rubric(terse)))
    assert any("levels 1 and 2 say the same thing" in i for i in issues)
    assert any("terse" in i for i in issues)

    circular = [{"metric": "Reviewer rubric score", "unit": "points", "operator": ">=", "value": i} for i in range(11)]
    issues = rubric.lint_rubric(KpiDraft(name="K", guidelines=_rubric(GOOD_LEVELS, circular)))
    assert any("restate the level number" in i for i in issues)

    gate = [{"metric": "declarations", "operator": ">=", "value": min(i, 2)} for i in range(11)]
    issues = rubric.lint_rubric(KpiDraft(name="K", guidelines=_rubric(GOOD_LEVELS, gate)))
    assert any("distinct thresholds" in i for i in issues)

    fallback = sb._fallback_guidelines("K")
    assert rubric.lint_rubric(KpiDraft(name="K", guidelines=fallback))[-1].startswith("generic fallback")


def test_targets_come_from_names_a_category_or_everything() -> None:
    draft = _draft()
    kpis, problems = rubric.select_targets(draft, None, "Quality", limit=30)
    assert [k.name for k in kpis] == ["Tests", "Docs"] and problems == []
    kpis, problems = rubric.select_targets(draft, ["demo", "Nope", "Delivery"], None, limit=30)
    assert [k.name for k in kpis] == ["Demo"]
    assert 'unknown KPI "Nope"' in problems and any("is a category" in p for p in problems)
    kpis, _ = rubric.select_targets(draft, None, None, limit=30)
    assert [k.name for k in kpis] == ["Milestones", "Status", "Tests", "Docs", "Demo"]
    kpis, problems = rubric.select_targets(draft, None, None, limit=2)
    assert len(kpis) == 2 and "only the first 2" in problems[0]


def test_set_levels_changes_only_the_given_levels_and_keeps_their_thresholds() -> None:
    draft = _draft()
    crit = {"metric": "milestones", "operator": ">=", "value": 4}
    draft = rubric._replace_guidelines(draft, {"Milestones": _rubric(GOOD_LEVELS, [None] * 4 + [crit] + [None] * 6)})
    new, text = rubric.set_levels(draft, {"edits": [
        {"name": "Milestones", "levels": {"4": {"text": "Four milestones documented, each with a dated status"},
                                          "5": {"text": "Five milestones with dated status and owner",
                                                "criteria": {"metric": "milestones", "value": 5}}}},
    ]})
    assert new is not None and text.startswith("Updated 2 level(s) in 1 KPI(s).")
    g = next(k for k in new.kpis if k.name == "Milestones").guidelines
    assert g["4"].qualitative_text.startswith("Four") and g["4"].quantitative_criteria == crit  # omitted: kept
    assert g["5"].quantitative_criteria == {"metric": "milestones", "operator": ">=", "value": 5}
    assert g["6"].qualitative_text == GOOD_LEVELS[6]  # untouched


def test_set_levels_is_all_or_nothing() -> None:
    draft = _draft()
    new, text = rubric.set_levels(draft, {"edits": [
        {"name": "Tests", "levels": {"3": {"text": "T 4"}}},  # would duplicate level 4
        {"name": "Docs", "levels": {"3": {"text": "Three documents"}}},
        {"name": "Quality", "levels": {"3": {"text": "x"}}},
    ]})
    assert new is None and text.startswith("REJECTED")
    assert "two levels would have the same text" in text and "category" in text


# --- graph level ------------------------------------------------------------------------------------------------


async def _seeded(draft: ScorecardDraft) -> str:
    session_id = str(uuid.uuid4())
    await sb.seed_session(session_id, draft, "Refine my scorecard", "Loaded it.")
    return session_id


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    return [
        block["toolResult"]["content"][0]["text"]
        for m in messages if m["role"] == "user"
        for block in m["content"] if "toolResult" in block
    ]


async def test_research_then_rewrite_a_category_uses_the_research_and_keeps_failed_kpis() -> None:
    steps = iter([
        tool_use_result("research_kpis", {"kpis": [{"name": "Tests", "query": "hackathon test coverage rubric"}]}),
        tool_use_result("rewrite_guidelines", {"category": "Quality", "instructions": "count tested modules"}),
        text_result("Rewrote the Quality rubrics. Save the draft to update the chart."),
    ])
    seen: list[list[str]] = []

    def agent_fn(*, messages, **_):
        seen.append(_tool_results(messages))
        return next(steps)

    prompts: list[str] = []

    def builder(*, system, **_):
        prompts.append(system or "")
        names = [line.split('"')[1] for line in (system or "").splitlines() if line.startswith('- "')]
        compact = [
            {"name": n, "suggested_weight": 5, "metric": "modules tested", "unit": "%", "direction": "higher_better",
             "levels": [f"{n}: {i * 10}% of modules have automated tests that run" for i in range(11)],
             "thresholds": [i * 10 for i in range(11)]}
            for n in names if n == "Tests"  # "Docs" gets nothing back: the writer fails for it
        ]
        return tool_use_result("fill_user_kpi_details", {"kpis": compact})

    search = FakeWebSearchClient(
        search_fn=lambda q: [search_result("Rubric", "https://example.com/r", "teams test 60% of modules")]
    )
    fake = FakeBedrockClient(agent_fn=agent_fn, converse_fn=builder)
    session_id = await _seeded(_draft())
    turn = await sb.send_message(session_id, "fix the Quality rubrics", fake, web_search_client=search)

    kpis = {k["name"]: k for k in turn.draft["kpis"]}
    assert kpis["Tests"]["guidelines"]["6"]["qualitative_text"] == "Tests: 60% of modules have automated tests that run"
    assert kpis["Docs"]["guidelines"] == full_rubric("D")  # writer failed: old rubric kept, not the fallback
    assert kpis["Milestones"]["guidelines"] == full_rubric("M")  # outside the category: untouched
    assert sum(k["weight"] or 0 for k in turn.draft["kpis"]) == 100  # weights untouched
    rewrite_result = seen[2][-1]
    assert rewrite_result.startswith("Rewrote 1 of 2") and "failed for Docs" in rewrite_result
    tests_prompt = next(p for p in prompts if '- "Tests"' in p)
    assert "teams test 60% of modules" in tests_prompt  # research reached the writer
    assert "count tested modules" in tests_prompt and "Current rubric: 0: T 0" in tests_prompt
    assert turn.assistant_note.startswith("Rewrote the Quality rubrics")
    assert search.queries == ["hackathon test coverage rubric"]


async def test_exact_level_edits_apply_inline_and_a_bad_one_comes_back() -> None:
    steps = iter([
        tool_use_result("set_kpi_guidelines", {"edits": [{"name": "Demo", "levels": {"2": {"text": "X 3"}}}]}),
        tool_use_result("set_kpi_guidelines", {"edits": [{"name": "Demo", "levels": {
            "2": {"text": "Demo video shows 2 of the 5 core flows working end to end"}}}]}),
        text_result("Level 2 of Demo now names the flows shown."),
    ])
    seen: list[list[str]] = []

    def agent_fn(*, messages, **_):
        seen.append(_tool_results(messages))
        return next(steps)

    fake = FakeBedrockClient(agent_fn=agent_fn)
    session_id = await _seeded(_draft())
    turn = await sb.send_message(session_id, "make level 2 of Demo concrete", fake)
    assert seen[1][-1].startswith("REJECTED") and seen[2][-1].startswith("Updated 1 level(s)")
    demo = next(k for k in turn.draft["kpis"] if k["name"] == "Demo")
    assert demo["guidelines"]["2"]["qualitative_text"].startswith("Demo video shows 2 of the 5")
    assert demo["guidelines"]["3"]["qualitative_text"] == "X 3"
    assert fake.calls == []  # no builder model call for an exact edit


async def test_review_reports_measured_scorer_confidence_for_the_chart_being_edited(async_db_session) -> None:
    owner, scorecard, version, nodes = await seed_scorecard(async_db_session, ("Innovation", "Execution"))
    for conf_innovation, conf_execution in ((0.2, 0.9), (0.3, 0.8)):
        ev = Evaluation(scorecard_version_id=version.id, name="E", evaluated_by=owner.id,
                        status=EvaluationStatus.COMPLETED)
        async_db_session.add(ev)
        await async_db_session.flush()
        for node, conf in zip(nodes, (conf_innovation, conf_execution), strict=True):
            async_db_session.add(EvaluationKpiResult(
                evaluation_id=ev.id, kpi_node_id=node.id, score=5, matched_guideline_level=5,
                jev_raw={"position": 4, "confidence": conf, "noul": 0.9},
            ))
    chat = ChatSession(user_id=owner.id, target_scorecard_id=scorecard.id)
    async_db_session.add(chat)
    await async_db_session.commit()

    draft = ScorecardDraft.model_validate({
        "name": "AI Scorecard", "purpose": "p", "domain": "Hackathon", "audience": "a", "target_score": 6,
        "kpis": [{"name": n.name, "weight": 50, "level": 1, "guidelines": full_rubric(n.name)} for n in nodes],
    })
    results: list[str] = []
    steps = iter([tool_use_result("review_rubrics", {}), text_result("Innovation is the weak one.")])

    def agent_fn(*, messages, **_):
        results.extend(_tool_results(messages))
        return next(steps)

    session_id = str(chat.id)
    await sb.seed_session(session_id, draft, "Refine my scorecard", "Loaded it.")
    await sb.send_message(session_id, "which KPIs score unreliably?", FakeBedrockClient(agent_fn=agent_fn))

    review = results[-1]
    assert "Low confidence: Innovation." in review
    rows = json.loads(review.splitlines()[-1])
    assert [r["name"] for r in rows] == ["Innovation", "Execution"]  # worst first
    assert rows[0]["scorer"] == {"evaluations": 2, "avg_confidence": 0.25, "low_confidence_share": 1.0,
                                 "avg_score": 5.0}
