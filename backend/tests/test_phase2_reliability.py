"""Phase 2: end-turn-after-edit with a server-built summary, near-duplicate collapsing, restricted tool
set after research, silent-drop guard, truncated-finding retry, header fill, measurable proxy metrics and
follow-up side questions."""

from __future__ import annotations

import re
import uuid

import pytest

import app.ai.scorecard_builder as sb
from app.ai.bedrock_client import ConverseResult
from app.ai.draft_schema import ScorecardDraft
from tests.fakes import FakeBedrockClient, FakeJevClient, FakeWebSearchClient, full_rubric, tool_use_result


def _compact(name: str) -> dict:
    return {
        "name": name, "suggested_weight": 5, "metric": f"{name} metric", "unit": "%", "direction": "higher_better",
        "levels": [f"{name}: condition {i}" for i in range(11)], "thresholds": [i * 10 for i in range(11)],
    }


def _fill_fn():
    def converse(*, system, tools, **_kw):
        wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
        return tool_use_result("fill_user_kpi_details", {"kpis": [_compact(n) for n in wanted]})

    return converse


def _big(n: int) -> list[dict]:
    return [
        {"name": f"KPI {i}", "weight": 100.0 / n, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric(f"K{i}")}
        for i in range(n)
    ]


def _total(kpis: list[dict]) -> float:
    return sum(k["weight"] for k in kpis)


def _edit_state(kpis: list[dict], ops: list[dict], text: str, **extra) -> dict:
    return {
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
        "messages": [{"role": "user", "content": text}],
        "pending_tool": {"name": "edit_kpis", "input": {"ops": ops, "assistant_message": "Done.", **extra}},
        "user_spec": None,
    }


# --- (4) end the turn after an edit; server-built summary; duplicate collapsing ----------------------------


def test_applied_edit_summary_uses_real_weights_and_drops_digit_laden_model_prose() -> None:
    state = _edit_state(
        _big(4), [{"op": "set_weight", "name": "KPI 1", "weight": 40}], "make KPI 1 40%, scale the others",
        assistant_message="KPI 1 is now 39.99% and the rest 20.01%",  # model quotes wrong numbers
    )
    out = sb.update_draft(state)
    note = out["messages"][0]["content"]
    assert "39.99" not in note and "20.01" not in note
    assert "KPI 1: 25.00% → 40.00%" in note and "Total weight of the scored KPIs: 100.00%" in note
    assert note.count("KPI 1:") == 1
    assert sb._route_after_update({**state, **out}) == "respond_conversationally"
    assert out["pending_tool"]["input"]["response"] == note


def test_confirming_edit_still_routes_to_confirm() -> None:
    kpis = _big(4)
    state = _edit_state(
        kpis, [{"op": "set_weight", "name": "KPI 1", "weight": 40}], "make KPI 1 40% and save it", confirmed=True
    )
    out = sb.update_draft(state)
    assert sb._route_after_update({**state, **out}) == "confirm"


def test_near_duplicate_paragraphs_with_drifting_numbers_collapse_to_one() -> None:
    base = "I've updated Customer Satisfaction Score to {n}% and proportionally scaled down all other KPI weights."
    messages = [{"role": "user", "content": "x"}] + [
        {"role": "assistant", "content": base.format(n=n) + extra}
        for n, extra in ((8, ""), (8.0, " Every KPI name remains."), (8.01, " Every KPI name remains exactly."))
    ] + [{"role": "assistant", "content": "Anything else to change?"}]
    note = sb._visible_turn_notes(messages)
    assert note.count("proportionally scaled") == 1 and note.endswith("Anything else to change?")


# --- (3) ---------------------------------------------------------------------------------------------------


def test_update_draft_that_silently_drops_several_kpis_is_rejected_but_user_requests_pass() -> None:
    kpis = _big(8)
    patch_kpis = [{"name": k["name"], "weight": 100 / 3, "level": 1} for k in kpis[:3]]

    def state(text: str) -> dict:
        return {
            "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
            "messages": [{"role": "user", "content": text}],
            "pending_tool": {
                "name": "update_draft", "input": {"patch": {"kpis": patch_kpis}, "assistant_message": "m"}
            },
        }

    rejected = sb.update_draft(state("looks fine"))
    assert "REJECTED" in rejected["messages"][0]["content"] and "draft" not in rejected
    assert len(rejected["messages"][0]["content"]) < 400
    assert "draft" in sb.update_draft(state("let's start over with a smaller set"))


async def test_truncated_research_finding_is_retried_once_tersely() -> None:
    seen: list[str] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        seen.append(system)
        if len(seen) == 1:
            return ConverseResult(stop_reason="max_tokens", tool_name="record_research_finding", tool_input={})
        return tool_use_result("record_research_finding", {"summary": "ok", "suggested_kpis": [], "sources": []})

    finding = await sb._run_research_agent(
        "Cat", "focus", FakeBedrockClient(converse_fn=fn), FakeWebSearchClient(search_fn=lambda q: []), None,
        session_id=str(uuid.uuid4()), turn_started_at=None, actor="research_agent_1",
        propose_batch=False, max_searches=0, use_quality_gate=False,
    )
    assert finding.summary == "ok" and not finding.degraded
    assert len(seen) == 2 and "CUT OFF" in seen[1]


def test_dynamic_coverage_hint_flags_thin_results() -> None:
    hint = sb._dynamic_coverage_hint(9, 5)
    assert "9 distinct KPIs" in hint and "INSUFFICIENT" in hint


async def test_category_proposal_prompt_asks_for_a_research_driven_count() -> None:
    systems: list[str] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        systems.append(system)
        return tool_use_result("propose_kpi_batch", {"kpis": [], "has_more": False})

    await sb._propose_category_kpis(
        FakeBedrockClient(converse_fn=fn), None, category="C", focus="f",
        finding=sb.ResearchFinding(category="C", summary="s"), avoid_names=None, want=None,
    )
    assert "roughly 5-8" in systems[0]


@pytest.mark.usefixtures("_migrated_db")
async def test_open_ended_first_turn_has_header_audience_complete_draft_and_no_update_draft_tool() -> None:
    tools_by_call: list[set[str]] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        names = {t.name for t in tools or []}
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": [{"name": "Cat A", "focus": "f"}]})
        if "assess_research_coverage" in names:
            return tool_use_result(
                "assess_research_coverage", {"sufficient": True, "reasoning": "", "next_categories": []}
            )
        if "record_research_finding" in names:
            return tool_use_result("record_research_finding", {"summary": "s", "suggested_kpis": [], "sources": []})
        if "propose_kpi_batch" in names:
            kpis = [{"name": f"{w} Signal Quality", "weight": 50, "rationale": "r"} for w in ("Alpha", "Bravo")]
            return tool_use_result("propose_kpi_batch", {"kpis": kpis, "has_more": False})
        if "fill_user_kpi_details" in names:
            return _fill_fn()(system=system, tools=tools)
        tools_by_call.append(names)
        return tool_use_result("ask_clarification", {"question": "Save it?", "options": [], "missing_fields": []})

    import app.ai.scorecard_builder as builder

    turn = await builder.start_session(
        str(uuid.uuid4()), "Build a scorecard for support call quality", FakeBedrockClient(converse_fn=fn),
        web_search_client=FakeWebSearchClient(search_fn=lambda q: []),
    )
    assert turn.draft["audience"] and turn.draft["name"] and turn.draft["purpose"] and turn.draft["domain"]
    assert ScorecardDraft.model_validate(turn.draft).is_complete()
    assert tools_by_call and "update_draft" not in tools_by_call[0] and "edit_kpis" in tools_by_call[0]


def test_audience_is_optional_for_completeness() -> None:
    d = ScorecardDraft.model_validate(
        {"name": "n", "purpose": "p", "domain": "d", "target_score": 8,
         "kpis": [{"name": "A", "weight": 100, "guidelines": full_rubric()}]}
    )
    assert d.audience is None and d.is_complete()


# --- (2) measurable proxy metric ----------------------------------------------------------------------------


def _none_dir(name: str) -> dict:
    return {**_compact(name), "direction": "none", "thresholds": [None] * 11}


def _proxy_fn(calls: list[str], *, fixes_on_retry: bool):
    def converse(*, system, tools, **_kw):
        wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
        retry = "RETRY:" in (system or "")
        calls.append("retry" if retry else "first")
        good = retry and fixes_on_retry
        return tool_use_result(
            "fill_user_kpi_details", {"kpis": [(_compact(n) if good else _none_dir(n)) for n in wanted]}
        )

    return converse


async def test_kpis_without_numeric_criteria_are_re_requested_once_with_a_proxy_demand() -> None:
    calls: list[str] = []
    stats: dict[str, int] = {}
    out, fb = await sb._fill_missing_guidelines(
        [{"name": "Greeting Quality", "parent_name": "C", "guidance": ""}],
        FakeBedrockClient(converse_fn=_proxy_fn(calls, fixes_on_retry=True)), None, "ctx", None, stats=stats,
    )
    assert calls == ["first", "retry"] and fb == []
    assert sb._criteria_levels(out["Greeting Quality"]) >= sb.MIN_CRITERIA_LEVELS
    assert stats["no_numeric"] == 0


async def test_still_no_numbers_after_the_retry_gets_an_honest_marker_and_no_invented_values() -> None:
    calls: list[str] = []
    stats: dict[str, int] = {}
    out, _fb = await sb._fill_missing_guidelines(
        [{"name": "Rapport", "parent_name": "C", "guidance": ""}],
        FakeBedrockClient(converse_fn=_proxy_fn(calls, fixes_on_retry=False)), None, "ctx", None, stats=stats,
    )
    assert calls == ["first", "retry"]
    rub = out["Rapport"]
    assert len(rub) == 11 and all(r["quantitative_criteria"] is None for r in rub.values())
    assert rub["10"]["qualitative_text"].endswith(sb.PROPOSED_TARGET_MARKER)
    assert stats == {"no_numeric": 1, "total": 1}


def test_fill_prompt_no_longer_offers_the_no_number_escape() -> None:
    assert 'never "none"' in sb._FILL_USER_KPIS_SYSTEM_PROMPT_TEMPLATE
    assert 'Use direction "none"' not in sb._FILL_USER_KPIS_SYSTEM_PROMPT_TEMPLATE


# --- follow-up side questions --------------------------------------------------------------------------------


def _follow_state(text: str, kpis: list[dict]) -> dict:
    return {
        "session_id": str(uuid.uuid4()),
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
        "messages": [
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": text},
        ],
        "llm_turn_count": 0,
    }


def _follow_cfg(note: str, answer: str | None):
    def fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("ask_clarification", {"question": note, "options": [], "missing_fields": []})

    def side(**_kw):
        return tool_use_result("answer_user_questions", {"answer": answer or ""})

    fake = FakeBedrockClient(converse_fn=fn, side_fn=side)
    return fake, {"configurable": {"bedrock_client": fake, "jev_client": FakeJevClient()}}


async def test_follow_up_question_gets_a_deterministic_answer_before_the_final_note() -> None:
    fake, cfg = _follow_cfg("Anything else?", "KPI 1 weighs more because it drives most defects.")
    out = await sb.propose_kpis(_follow_state("Why is KPI 1 weighted so high?", _big(3)), cfg)
    assert [m["content"] for m in out["messages"]] == [
        "KPI 1 weighs more because it drives most defects.", "Anything else?",
    ]
    assert len(fake.side_calls) == 1


async def test_edit_instruction_phrased_as_a_question_is_not_treated_as_a_side_question() -> None:
    fake, cfg = _follow_cfg("Done?", "should not appear")
    out = await sb.propose_kpis(_follow_state("Can you make KPI 1 8% please?", _big(3)), cfg)
    assert fake.side_calls == [] and [m["content"] for m in out["messages"]] == ["Done?"]


async def test_side_answer_is_dropped_when_the_models_note_already_gives_it() -> None:
    answer = "KPI 1 weighs more because it drives most of the defects found in review."
    _fake, cfg = _follow_cfg("Because " + answer, answer)
    out = await sb.propose_kpis(_follow_state("Why is KPI 1 weighted so high?", _big(3)), cfg)
    assert len(out["messages"]) == 1
