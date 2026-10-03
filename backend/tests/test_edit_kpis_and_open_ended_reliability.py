"""Open-ended pipeline reliability + incremental edits:

- a leaf can never be confirmed/saved with fewer than 11 guideline levels; incomplete KPIs are repaired
  automatically through the compact-rubric machinery (fallback rubric only as a last resort),
- research agents propose names/weights/rationales only; rubrics are written in parallel compact chunks,
- the quality gate has a per-call timeout, a per-turn time budget and is skipped for complete drafts,
- `edit_kpis` applies small operations server-side (exact weight math, pinned rules, O(1) model output),
- every assistant-visible note of a turn (explanation + question) is returned.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app.ai.scorecard_builder as sb
from app.ai.draft_schema import ScorecardDraft
from app.ai.jev_client import JevRatingResult, quality_gate
from app.config import get_settings
from tests.fakes import FakeBedrockClient, FakeJevClient, full_rubric, tool_use_result


def _compact(name: str) -> dict:
    return {
        "name": name, "suggested_weight": 5, "metric": f"{name} metric", "unit": "%", "direction": "higher_better",
        "levels": [f"{name}: condition {i}" for i in range(11)], "thresholds": [i * 10 for i in range(11)],
    }


def _fill_fn(calls: list[list[str]] | None = None, fail: bool = False):
    def converse(*, system, tools, **_kw):
        wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
        if calls is not None:
            calls.append(wanted)
        if fail:
            raise RuntimeError("bedrock down")
        return tool_use_result("fill_user_kpi_details", {"kpis": [_compact(n) for n in wanted]})

    return converse


def _partial(n_levels: int) -> dict:
    return {str(i): {"qualitative_text": f"level {i}", "quantitative_criteria": None} for i in range(n_levels)}


# --- 11 levels are required for completeness -------------------------------------------------------


def _draft(kpis: list[dict]) -> ScorecardDraft:
    return ScorecardDraft.model_validate(
        {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis}
    )


def test_partial_guidelines_make_the_draft_incomplete() -> None:
    d = _draft([{"name": "A", "weight": 100, "guidelines": _partial(6)}])
    assert not d.is_complete()
    assert any("A].guidelines (6/11" in m for m in d.missing_fields())
    assert _draft([{"name": "A", "weight": 100, "guidelines": full_rubric()}]).is_complete()


def test_update_draft_cannot_confirm_with_partial_guidelines() -> None:
    state = {
        "draft": ScorecardDraft().model_dump(mode="json"),
        "messages": [{"role": "user", "content": "save it"}],
        "pending_tool": {
            "name": "update_draft",
            "input": {
                "patch": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8,
                          "kpis": [{"name": "A", "weight": 100, "guidelines": _partial(4)}]},
                "confirmed": True, "assistant_message": "ok",
            },
        },
    }
    assert sb.update_draft(state)["status"] == "gathering"


# --- proposals are names-only; rubrics are written in parallel compact chunks -------------------------


def _category_fn(names: list[str], *, partial_for: set[str] | None = None, fill=None):
    partial_for = partial_for or set()

    def converse(*, system, tools, **kw):
        tool_names = {t.name for t in tools or []}
        if "propose_kpi_batch" in tool_names:
            assert "guidelines" not in tool_names  # names-only schema
            schema = [t for t in tools if t.name == "propose_kpi_batch"][0].input_schema
            assert "guidelines" not in schema["properties"]["kpis"]["items"]["properties"]
            kpis = [{"name": n, "weight": 10, "rationale": f"measures {n}"} for n in names]
            for k in kpis:
                if k["name"] in partial_for:
                    k["guidelines"] = _partial(3)  # a model ignoring the schema and stopping early
            return tool_use_result("propose_kpi_batch", {"kpis": kpis, "has_more": False})
        return fill(system=system, tools=tools, **kw)

    return converse


async def test_category_kpis_get_complete_rubrics_from_parallel_compact_chunks() -> None:
    words = "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet Kilo Lima".split()
    names = [f"{w} Quality Signal" for w in words]
    calls: list[list[str]] = []
    out = await sb._propose_category_kpis(
        FakeBedrockClient(converse_fn=_category_fn(names, fill=_fill_fn(calls))), None,
        category="Cat", focus="f", finding=sb.ResearchFinding(category="Cat", summary="s"),
        avoid_names=None, want=None,
    )
    assert [k["name"] for k in out] == names
    assert all(len(k["guidelines"]) == 11 for k in out)
    assert not any("Auto-generated fallback" in k["guidelines"]["5"]["qualitative_text"] for k in out)
    per_call = get_settings().user_kpis_per_fill_call
    assert len(calls) == -(-len(names) // per_call) and max(map(len, calls)) <= per_call


async def test_partial_guidelines_from_the_model_are_repaired_never_kept() -> None:
    """Regression for the live defect: KPIs came back with only some of the 11 levels."""
    names = [f"Opening {i}" for i in range(5)]
    out = await sb._propose_category_kpis(
        FakeBedrockClient(converse_fn=_category_fn(names, partial_for=set(names), fill=_fill_fn())), None,
        category="Opening & Qualification", focus="f",
        finding=sb.ResearchFinding(category="x", summary="s"), avoid_names=None, want=None,
    )
    assert len(out) == 5 and all(len(k["guidelines"]) == 11 for k in out)
    assert all(k["guidelines"]["3"]["qualitative_text"].endswith("condition 3") for k in out)  # regenerated
    merged = sb._merge_research_kpi_batches(
        [sb.ResearchFinding(category="Opening & Qualification", summary="s", proposed_kpis=out)], {}
    )
    assert _draft(merged).is_complete()


async def test_last_resort_fallback_rubric_is_complete_and_marked() -> None:
    names = ["A KPI", "B KPI"]
    out = await sb._propose_category_kpis(
        FakeBedrockClient(converse_fn=_category_fn(names, fill=_fill_fn(fail=True))), None,
        category="Cat", focus="f", finding=sb.ResearchFinding(category="Cat", summary="s"),
        avoid_names=None, want=None,
    )
    assert all(len(k["guidelines"]) == 11 for k in out)
    assert all("Auto-generated fallback" in k["guidelines"]["0"]["qualitative_text"] for k in out)


async def test_repair_incomplete_leaves_safety_net_only_touches_incomplete_leaves() -> None:
    calls: list[list[str]] = []
    kpis = [
        {"name": "Cat", "weight": None, "level": 1, "parent_name": None, "included_in_scoring": True, "guidelines": {}},
        {"name": "Good", "weight": 50, "level": 2, "parent_name": "Cat", "included_in_scoring": True,
         "guidelines": full_rubric()},
        {"name": "Short", "weight": 50, "level": 2, "parent_name": "Cat", "included_in_scoring": True,
         "guidelines": _partial(2)},
    ]
    fixed, fb = await sb._repair_incomplete_leaves(
        kpis, FakeBedrockClient(converse_fn=_fill_fn(calls)), None, "ctx"
    )
    assert calls == [["Short"]] and fb == []
    assert fixed[1]["guidelines"] == full_rubric() and len(fixed[2]["guidelines"]) == 11
    assert fixed[0]["guidelines"] == {}


# --- quality gate cost control ------------------------------------------------------------------------


async def test_slow_jev_call_times_out_and_degrades_to_passed(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "quality_gate_call_timeout_seconds", 0.05)

    async def slow(**_kw):
        await asyncio.sleep(5)
        return JevRatingResult(score=0.1)

    jev = FakeJevClient(rate_fn=lambda **kw: 0.1)
    jev.rate_match = slow  # type: ignore[method-assign]
    gate = await quality_gate(jev, instruction="i", answer="a")
    assert gate.passed and gate.degraded


async def test_gate_is_skipped_once_the_turn_time_budget_is_spent() -> None:
    jev = FakeJevClient(rate_fn=lambda **kw: 0.1)
    old = datetime.now(UTC) - timedelta(seconds=get_settings().quality_gate_budget_seconds + 5)
    result = await sb._gate(jev, instruction="i", answer="a", turn_started_at=old)
    assert result.passed and result.degraded and jev.calls == []
    fresh = await sb._gate(jev, instruction="i", answer="a", turn_started_at=datetime.now(UTC))
    assert not fresh.passed and len(jev.calls) == 1


def test_default_gate_revisions_is_one() -> None:
    assert sb.MAX_QUALITY_GATE_RETRIES == 1


# --- edit_kpis --------------------------------------------------------------------------------------


def _big(n: int = 35) -> list[dict]:
    return [
        {"name": f"KPI {i}", "weight": 100.0 / n, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric(f"K{i}")}
        for i in range(n)
    ]


def _total(kpis: list[dict]) -> float:
    parents = {k["parent_name"] for k in kpis if k["parent_name"]}
    return sum(k["weight"] for k in kpis if k["name"] not in parents and k["included_in_scoring"])


def test_set_weight_rebalances_the_others_proportionally_to_exactly_100() -> None:
    kpis = _big()
    kpis[3]["weight"], kpis[4]["weight"] = 5.0, 10.0
    kpis = [k for k in kpis]
    total_before = _total(kpis)
    kpis[0]["weight"] += 100 - total_before  # make the start exactly 100
    new, errors, targeted, changed = sb._apply_kpi_ops(kpis, [{"op": "set_weight", "name": "KPI 3", "weight": 8}], {})
    assert errors == [] and targeted == {"KPI 3"}
    by = {k["name"]: k for k in new}
    assert by["KPI 3"]["weight"] == 8
    assert _total(new) == pytest.approx(100.0, abs=1e-9)
    ratio = by["KPI 4"]["weight"] / by["KPI 5"]["weight"]
    assert ratio == pytest.approx(10.0 / kpis[5]["weight"], rel=0.02)  # relative proportions kept
    assert all(by[k["name"]]["guidelines"] == k["guidelines"] for k in kpis)
    assert "KPI 4" in changed


def test_set_weight_without_rebalance_leaves_others_alone() -> None:
    new, errors, *_ = sb._apply_kpi_ops(
        _big(4), [{"op": "set_weight", "name": "KPI 1", "weight": 40, "rebalance": "none"}], {}
    )
    assert errors == [] and [k["weight"] for k in new] == [25.0, 40, 25.0, 25.0]


def test_remove_add_rename_and_move() -> None:
    kpis = [
        {"name": "Cat A", "weight": None, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": {}},
        {"name": "Cat B", "weight": None, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": {}},
        {"name": "X", "weight": 40, "level": 2, "parent_name": "Cat A", "included_in_scoring": True,
         "guidelines": full_rubric("x")},
        {"name": "Y", "weight": 40, "level": 2, "parent_name": "Cat A", "included_in_scoring": True,
         "guidelines": full_rubric("y")},
        {"name": "Z", "weight": 20, "level": 2, "parent_name": "Cat B", "included_in_scoring": True,
         "guidelines": full_rubric("z")},
    ]
    gen = {"New KPI": full_rubric("new")}
    new, errors, *_ = sb._apply_kpi_ops(
        kpis,
        [
            {"op": "remove", "name": "Y"},
            {"op": "rename", "name": "X", "new_name": "X2"},
            {"op": "add", "name": "New KPI", "parent_name": "Cat B", "weight": 10, "rationale": "r"},
            {"op": "move", "name": "Z", "parent_name": "Cat A"},
        ],
        gen,
    )
    assert errors == []
    by = {k["name"]: k for k in new}
    assert "Y" not in by and "X" not in by and by["X2"]["guidelines"] == full_rubric("x")
    assert by["New KPI"]["weight"] == 10 and by["New KPI"]["guidelines"] == full_rubric("new")
    assert by["New KPI"]["level"] == 2 and by["Z"]["parent_name"] == "Cat A"
    assert _total(new) == pytest.approx(100.0, abs=1e-9)
    assert _draft(new).is_complete()


def test_bad_ops_are_reported_not_applied() -> None:
    _new, errors, *_ = sb._apply_kpi_ops(_big(3), [{"op": "set_weight", "name": "Nope", "weight": 5}], {})
    assert errors and "Nope" in errors[0]
    _new, errors, *_ = sb._apply_kpi_ops(_big(3), [{"op": "add", "name": "KPI 1"}], {})
    assert errors


def _state(kpis: list[dict], ops: list[dict], user_text: str, pinned: list[dict] | None = None, **extra) -> dict:
    return {
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
        "messages": [{"role": "user", "content": user_text}],
        "pending_tool": {"name": "edit_kpis", "input": {"ops": ops, "assistant_message": "Done.", **extra}},
        "user_spec": {"mode": "user_specified", "pinned": pinned or [], "notes": []} if pinned is not None else None,
    }


def _pins(kpis: list[dict]) -> list[dict]:
    return [
        {"name": k["name"], "parent_name": None, "level": 1, "weight": k["weight"], "guidelines_hash": None}
        for k in kpis
    ]


def test_exact_follow_up_scenario_on_a_35_kpi_user_scorecard() -> None:
    """'Make that KPI 8% and scale the others down proportionally' — one tiny op, exact 100, pins refreshed."""
    kpis = _big(35)
    kpis[4]["weight"] = 5.0
    rest = 95.0 / 34
    for k in kpis[:4] + kpis[5:]:
        k["weight"] = round(rest, 2)
    kpis[5]["weight"] = round(95.0 - sum(k["weight"] for k in kpis[:4] + kpis[6:]), 2)
    op = [{"op": "set_weight", "name": "KPI 4", "weight": 8}]
    msg = "Make KPI 4 8% and scale the others down so the total stays 100"
    out = sb.update_draft(_state(kpis, op, msg, _pins(kpis)))
    assert out["status"] == "gathering" and "REJECTED" not in out["messages"][0]["content"]
    new = out["draft"]["kpis"]
    assert _total(new) == pytest.approx(100.0, abs=1e-9)
    assert {k["name"] for k in new} == {k["name"] for k in kpis}  # names untouched
    assert all(len(k["guidelines"]) == 11 for k in new)  # rubrics preserved
    assert next(k for k in new if k["name"] == "KPI 4")["weight"] == 8
    pin0 = next(p for p in out["user_spec"]["pinned"] if p["name"] == "KPI 4")
    assert pin0["weight"] == 8  # the user's new value is frozen
    summary = out["messages"][0]["content"]
    assert summary.startswith("Done.") and "KPI 4: 5.00% → 8.00%" in summary and "100.00%" in summary
    assert out["pending_tool"]["name"] == "respond_conversationally"  # the edit ends the turn


def test_editing_a_pinned_kpi_the_user_did_not_mention_is_rejected() -> None:
    kpis = _big(5)
    op = [{"op": "remove", "name": "KPI 2"}]
    out = sb.update_draft(_state(kpis, op, "looks good, thanks", _pins(kpis)))
    assert "draft" not in out and "REJECTED" in out["messages"][0]["content"]


def test_non_pinned_kpis_edit_freely_and_rebalance_around_pinned_when_asked() -> None:
    kpis = _big(4)
    out = sb.update_draft(_state(kpis, [{"op": "remove", "name": "KPI 1"}], "remove KPI 1", _pins(kpis)))
    new = out["draft"]["kpis"]
    assert [k["name"] for k in new] == ["KPI 0", "KPI 2", "KPI 3"] and _total(new) == pytest.approx(100.0, abs=1e-9)
    assert all(p["name"] != "KPI 1" for p in out["user_spec"]["pinned"])  # pin released


def test_full_replacement_that_omits_guidelines_preserves_existing_rubrics() -> None:
    kpis = _big(3)
    patch_kpis = [{"name": k["name"], "weight": k["weight"], "level": 1} for k in kpis]
    patch_kpis[0]["guidelines"] = _partial(2)
    state = {
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
        "messages": [{"role": "user", "content": "x"}],
        "pending_tool": {"name": "update_draft", "input": {"patch": {"kpis": patch_kpis}, "assistant_message": "m"}},
    }
    out = sb.update_draft(state)
    assert all(k["guidelines"] == full_rubric(f"K{i}") for i, k in enumerate(out["draft"]["kpis"]))


@pytest.mark.usefixtures("_migrated_db")
async def test_edit_kpis_model_output_is_constant_size_on_a_huge_scorecard_and_generates_new_rubrics() -> None:
    kpis = _big(120)
    sent: dict = {}
    fill_calls: list[list[str]] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        names = {t.name for t in tools or []}
        if "edit_kpis" in names:
            assert "edit_kpis" in names and "update_draft" in names
            sent["ops"] = [
                {"op": "set_weight", "name": "KPI 7", "weight": 2},
                {"op": "add", "name": "Brand New KPI", "parent_name": None, "weight": 1, "rationale": "new thing"},
            ]
            return tool_use_result("edit_kpis", {"ops": sent["ops"], "assistant_message": "Updated."})
        return _fill_fn(fill_calls)(system=system, tools=tools)

    state = {
        "session_id": str(uuid.uuid4()),
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
        "messages": [{"role": "user", "content": "set KPI 7 to 2% and add a Brand New KPI at 1%"}],
        "llm_turn_count": 0,
    }
    cfg = {"configurable": {"bedrock_client": FakeBedrockClient(converse_fn=fn), "jev_client": FakeJevClient()}}
    node = await sb.propose_kpis(state, cfg)
    assert node["pending_tool"]["name"] == "edit_kpis"
    assert len(str(sent["ops"])) < 400  # O(ops), independent of the 120 KPIs
    assert fill_calls == [["Brand New KPI"]]  # only the new KPI's rubric is generated
    assert not _jev_called(cfg)
    after = {**state, "pending_tool": node["pending_tool"], "messages": state["messages"] + node["messages"]}
    out = sb.update_draft(after)
    new = out["draft"]["kpis"]
    assert len(new) == 121 and _total(new) == pytest.approx(100.0, abs=1e-9)
    assert len(next(k for k in new if k["name"] == "Brand New KPI")["guidelines"]) == 11


def _jev_called(cfg) -> bool:
    return bool(cfg["configurable"]["jev_client"].calls)


# --- every assistant-visible note of the turn is returned --------------------------------------------------


def test_update_draft_explanation_and_clarification_question_are_both_returned() -> None:
    messages = [
        {"role": "user", "content": "why is this scorecard needed? and build it"},
        {"role": "assistant", "content": "[research] internal note"},
        {"role": "assistant", "content": "It standardises call quality reviews, so I drafted 12 KPIs."},
        {"role": "tool", "content": "It standardises call quality reviews, so I drafted 12 KPIs."},
        {"role": "assistant", "content": "Would you like me to save it as-is?"},
    ]
    note = sb._visible_turn_notes(messages)
    assert note == "It standardises call quality reviews, so I drafted 12 KPIs.\n\nWould you like me to save it as-is?"


def test_generic_notes_and_previous_turns_are_not_shown() -> None:
    messages = [
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "go"},
        {"role": "tool", "content": "Draft updated."},
    ]
    assert sb._visible_turn_notes(messages) == "Draft updated."
    assert sb._visible_turn_notes([{"role": "user", "content": "hi"}]) is None


def test_unnamed_reference_to_the_kpi_just_discussed_is_verified_from_the_previous_assistant_message() -> None:
    """The live message: 'Make that KPI 8% and scale the others down proportionally'."""
    kpis = _big(6)
    state = _state(kpis, [{"op": "set_weight", "name": "KPI 2", "weight": 8}],
                   "Make that KPI 8% and scale the others down proportionally so the total stays 100", _pins(kpis))
    state["messages"] = [
        {"role": "assistant", "content": "KPI 2 is currently at 5%. Save as-is or change something?"},
        state["messages"][0],
    ]
    out = sb.update_draft(state)
    assert "REJECTED" not in out["messages"][0]["content"]
    assert _total(out["draft"]["kpis"]) == pytest.approx(100.0, abs=1e-9)


# --- visible notes never leak internal/superseded text ------------------------------------------------


def test_rejected_attempt_apology_and_internal_notes_never_reach_the_visible_message() -> None:
    """The live leak: apology + 'Done!' + 'edit_kpis REJECTED: ... (34 names)'."""
    messages = [
        {"role": "user", "content": "Make CSAT 8%"},
        {"role": "assistant", "content": "I'm sorry, but I encountered an issue while processing your request. "
                                         "Please try again or rephrase your request."},
        {"role": "assistant", "content": "Done! CSAT is now 8%."},
        {"role": "tool", "content": 'edit_kpis REJECTED: the user did not ask to change "A", "B"'},
        {"role": "tool", "content": "[quality gate] Your previous response was: ..."},
        {"role": "assistant", "content": "Done! CSAT is now 8%, others scaled."},
        {"role": "tool", "content": "Done! CSAT is now 8%, others scaled."},
        {"role": "assistant", "content": "Anything else to change?"},
    ]
    assert sb._visible_turn_notes(messages) == "Done! CSAT is now 8%, others scaled.\n\nAnything else to change?"
    only_internal = [{"role": "user", "content": "x"}, {"role": "tool", "content": "edit_kpis REJECTED: nope"}]
    assert sb._visible_turn_notes(only_internal) is None


# --- one op + "scale the others" ----------------------------------------------------------------------


def test_listing_every_other_kpi_after_scale_the_others_is_accepted_and_exact() -> None:
    kpis = _big(35)
    for k in kpis:
        k["weight"] = round(100 / 35, 4)
    kpis[0]["weight"] += round(100 - _total(kpis), 4)
    ops = [{"op": "set_weight", "name": "KPI 4", "weight": 8, "rebalance": "proportional"}] + [
        {"op": "set_weight", "name": k["name"], "weight": 2.7} for k in kpis if k["name"] != "KPI 4"
    ]
    msg = "Make KPI 4 8% and scale the others down proportionally so the total stays 100. Keep every other name."
    out = sb.update_draft(_state(kpis, ops, msg, _pins(kpis)))
    assert "REJECTED" not in out["messages"][0]["content"]
    new = out["draft"]["kpis"]
    assert next(k for k in new if k["name"] == "KPI 4")["weight"] == 8
    assert _total(new) == pytest.approx(100.0, abs=1e-9)
    assert {k["name"] for k in new} == {k["name"] for k in kpis}


def test_rejection_message_is_short_even_with_dozens_of_targets() -> None:
    kpis = _big(40)
    ops = [{"op": "set_weight", "name": k["name"], "weight": 2} for k in kpis]
    out = sb.update_draft(_state(kpis, ops, "looks fine", _pins(kpis)))
    msg = out["messages"][0]["content"]
    assert "REJECTED" in msg and "more" in msg and len(msg) < 400


def test_large_drafts_are_shown_to_the_model_without_rubrics() -> None:
    shown = sb._prompt_draft(_draft(_big(20)))
    assert all(isinstance(k["guidelines"], str) for k in shown["kpis"])
    assert isinstance(sb._prompt_draft(_draft(_big(3)))["kpis"][0]["guidelines"], dict)


# --- side questions are answered deterministically ----------------------------------------------------


def test_question_extraction() -> None:
    text = "Build a scorecard. I need 35 KPIs. Also, why is this scorecard needed in the first place?"
    qs = sb._extract_user_questions(text)
    assert qs == ["Also, why is this scorecard needed in the first place?"]
    assert sb._extract_user_questions("KPIs: Speed, Quality. No questions here.") == []


@pytest.mark.usefixtures("_migrated_db")
async def test_first_turn_side_question_is_answered_before_the_final_question() -> None:
    def propose(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("ask_clarification", {"question": "Save as-is?", "options": [], "missing_fields": []})

    def side(**_kw):
        return tool_use_result("answer_user_questions", {"answer": "It is needed to standardise call reviews."})

    fake = FakeBedrockClient(converse_fn=propose, side_fn=side)
    turn = await sb.start_session(
        str(uuid.uuid4()), "Scorecard for support calls. Also, why is this scorecard needed in the first place?", fake
    )
    assert turn.assistant_note == "It is needed to standardise call reviews."  # the card carries the question
    assert turn.question["question"] == "Save as-is?"
    assert len(fake.side_calls) == 1


@pytest.mark.usefixtures("_migrated_db")
async def test_failed_side_answer_degrades_to_the_plain_question() -> None:
    def propose(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("ask_clarification", {"question": "Save as-is?", "options": [], "missing_fields": []})

    def side(**_kw):
        raise RuntimeError("down")

    turn = await sb.start_session(
        str(uuid.uuid4()), "Scorecard for calls. Why do we need it at all?",
        FakeBedrockClient(converse_fn=propose, side_fn=side),
    )
    assert turn.assistant_note == "Save as-is?"
