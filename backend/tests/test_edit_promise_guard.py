"""The pipeline must not depend on the model picking the right tool for an edit instruction: edit-intent
turns offer only mutating tools, a 'let me apply that' reply is never surfaced (one nudge, then a
deterministic parser applies the common simple edits), and the visible text is the server summary."""

from __future__ import annotations

import uuid

import pytest

import app.ai.scorecard_builder as sb
from app.ai.draft_schema import ScorecardDraft
from tests.fakes import FakeBedrockClient, FakeJevClient, full_rubric, tool_use_result

PROMISE = (
    "I want to make sure I get this exactly right. Let me apply this change now with a single edit operation "
    "that will: 1. Set Customer Satisfaction Score to 8% 2. Automatically proportionally scale the others."
)
MESSAGE = (
    "Make Customer Satisfaction Score 8% and scale the others down proportionally so the total stays 100. "
    "Keep every other KPI name exactly as is."
)


def _kpis(n: int = 35) -> list[dict]:
    names = ["Customer Satisfaction Score"] + [f"Signal {chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(n - 1)]
    out = [
        {"name": nm, "weight": 100.0 / n, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric(nm)}
        for nm in names
    ]
    out[0]["weight"] = 3.91
    out[1]["weight"] = round(100 - 3.91 - sum(k["weight"] for k in out[2:]), 4)
    return out


def _state(text: str = MESSAGE, kpis: list[dict] | None = None) -> dict:
    return {
        "session_id": str(uuid.uuid4()),
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis or _kpis()},
        "messages": [
            {"role": "user", "content": "my KPIs ..."}, {"role": "assistant", "content": "Save as-is?"},
            {"role": "user", "content": text},
        ],
        "llm_turn_count": 0,
        "user_spec": None,
    }


def _run_cfg(fn):
    fake = FakeBedrockClient(converse_fn=fn)
    return fake, {"configurable": {"bedrock_client": fake, "jev_client": FakeJevClient()}}


def _total(draft: dict) -> float:
    return sum(k["weight"] for k in draft["kpis"])


async def _turn(state: dict, cfg) -> tuple[dict, dict]:
    node = await sb.propose_kpis(state, cfg)
    merged = {**state, **node, "messages": state["messages"] + node["messages"]}
    return node, sb.update_draft(merged)


def test_edit_intent_detection() -> None:
    d = ScorecardDraft.model_validate({"kpis": _kpis(4)})
    assert sb._has_edit_intent(MESSAGE, d.kpis)
    assert sb._has_edit_intent("Remove Signal AA please", d.kpis)
    assert sb._has_edit_intent("set it to 12%", d.kpis)
    assert not sb._has_edit_intent("Why is Customer Satisfaction Score weighted so high?", d.kpis)
    assert not sb._has_edit_intent("Looks great, thanks", d.kpis)
    assert not sb._has_edit_intent(MESSAGE, [])


def test_simple_edit_parser() -> None:
    d = ScorecardDraft.model_validate({"kpis": _kpis(4)})
    assert sb._parse_simple_edit(MESSAGE, d) == [
        {"op": "set_weight", "name": "Customer Satisfaction Score", "weight": 8.0, "rebalance": "proportional"}
    ]
    assert sb._parse_simple_edit("set customer satisfaction score to 12.5%", d)[0]["weight"] == 12.5
    assert sb._parse_simple_edit("rename Signal AA to Fast Reply", d) == [
        {"op": "rename", "name": "Signal AA", "new_name": "Fast Reply"}
    ]
    assert sb._parse_simple_edit("remove Signal AB.", d) == [{"op": "remove", "name": "Signal AB"}]
    assert sb._parse_simple_edit("make everything better", d) == []


async def test_promise_then_proper_edit_is_applied_and_the_promise_is_never_shown() -> None:
    calls: list[set[str]] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        calls.append({t.name for t in tools})
        if len(calls) == 1:
            return tool_use_result("respond_conversationally", {"response": PROMISE})
        return tool_use_result(
            "edit_kpis", {"ops": [{"op": "set_weight", "name": "Customer Satisfaction Score", "weight": 8}],
                          "assistant_message": "Done."},
        )

    _fake, cfg = _run_cfg(fn)
    node, out = await _turn(_state(), cfg)
    assert all("respond_conversationally" not in c for c in calls)  # never offered on an edit instruction
    assert len(calls) == 2  # one nudge, then the real edit
    new = out["draft"]
    assert next(k for k in new["kpis"] if k["name"] == "Customer Satisfaction Score")["weight"] == 8
    assert _total(new) == pytest.approx(100.0, abs=1e-9)
    assert sb._visible_turn_notes([{"role": "user", "content": "x"}, *node["messages"], *out["messages"]]) == (
        out["messages"][0]["content"]
    )
    assert "Let me apply" not in out["messages"][0]["content"]
    assert "Customer Satisfaction Score: 3.91% → 8.00%" in out["messages"][0]["content"]


async def test_model_that_only_promises_twice_gets_the_deterministic_edit() -> None:
    def fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("respond_conversationally", {"response": PROMISE})

    _fake, cfg = _run_cfg(fn)
    node, out = await _turn(_state(), cfg)
    assert node["pending_tool"]["name"] == "edit_kpis"
    new = out["draft"]
    assert next(k for k in new["kpis"] if k["name"] == "Customer Satisfaction Score")["weight"] == 8
    assert _total(new) == pytest.approx(100.0, abs=1e-9)
    assert {k["name"] for k in new["kpis"]} == {k["name"] for k in _kpis()}  # names untouched
    summary = out["messages"][0]["content"]
    assert out["messages"][0]["kind"] == "edit_summary" and summary.count("?") == 1
    assert out["pending_tool"]["name"] == "respond_conversationally"  # turn ends with the summary


async def test_ask_clarification_with_an_action_promise_is_also_caught() -> None:
    def fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("ask_clarification", {"question": "I'll make that change now, ok?", "options": []})

    _fake, cfg = _run_cfg(fn)
    node, out = await _turn(_state("Remove Signal AB", _kpis(6)), cfg)
    assert node["pending_tool"]["name"] == "edit_kpis"
    assert "Signal AB" not in {k["name"] for k in out["draft"]["kpis"]}
    assert _total(out["draft"]) == pytest.approx(100.0, abs=1e-9)


async def test_genuinely_ambiguous_clarification_is_still_allowed() -> None:
    def fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result("ask_clarification", {"question": "Which of the two do you mean?", "options": []})

    _fake, cfg = _run_cfg(fn)
    node = await sb.propose_kpis(_state(), cfg)
    assert node["pending_tool"]["name"] == "ask_clarification"


async def test_non_edit_follow_up_keeps_the_conversational_tool() -> None:
    calls: list[set[str]] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        calls.append({t.name for t in tools})
        return tool_use_result("respond_conversationally", {"response": "Because it drives churn."})

    _fake, cfg = _run_cfg(fn)
    node = await sb.propose_kpis(_state("Why is Customer Satisfaction Score weighted so high?"), cfg)
    assert "respond_conversationally" in calls[0] and node["pending_tool"]["name"] == "respond_conversationally"
