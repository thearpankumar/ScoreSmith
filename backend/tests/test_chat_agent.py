"""The chat assistant in front of the scorecard builder (app/ai/chat_agent.py): it answers ordinary messages itself,
edits / saves the draft with small tools, and hands the builder only the requests that need a scorecard drafted.
Graph-level tests against the real checkpointer, with the assistant's model scripted through `agent_fn`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.ai.chat_agent as agent
import app.ai.scorecard_builder as sb
from app.ai.bedrock_client import ConverseResult, ToolCall
from app.ai.draft_schema import ScorecardDraft
from tests.fakes import (
    FakeBedrockClient,
    FakeOpenRouterClient,
    FakeWebSearchClient,
    full_rubric,
    search_result,
    text_result,
    tool_use_result,
)


def _complete_draft(n: int = 4) -> ScorecardDraft:
    return ScorecardDraft.model_validate(
        {
            "name": "Call QA", "purpose": "Rate support calls", "domain": "Support", "audience": "QA team",
            "target_score": 8,
            "kpis": [
                {"name": f"KPI {i}", "weight": 100 / n, "level": 1, "guidelines": full_rubric(f"KPI {i}")}
                for i in range(n)
            ],
        }
    )


async def _seeded(draft: ScorecardDraft | None = None) -> str:
    session_id = str(uuid.uuid4())
    await sb.seed_session(session_id, draft or _complete_draft(), "Refine my scorecard", "Loaded it.")
    return session_id


async def _state(session_id: str) -> dict[str, Any]:
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    return snapshot.values


def _leaf_total(draft: dict) -> float:
    return sum(k["weight"] or 0 for k in draft["kpis"])


# --- answering without the builder ----------------------------------------------------------------------------


async def test_a_greeting_is_answered_without_running_the_builder() -> None:
    fake = FakeBedrockClient(agent_fn=lambda **_: text_result("Hi! I can help you design a quality scorecard."))
    session_id = str(uuid.uuid4())
    turn = await sb.start_session(session_id, "hello", fake, web_search_client=FakeWebSearchClient())
    assert turn.status == "gathering" and turn.question is None
    assert turn.assistant_note == "Hi! I can help you design a quality scorecard."
    assert len(fake.agent_calls) == 1 and fake.calls == [] and fake.classify_calls == []
    state = await _state(session_id)
    # The research fan-out has NOT been spent on small talk: a later build request still gets it.
    assert not state.get("research_done") and not state.get("similarity_checked")
    assert state["agent_route"] == "answer"


async def test_the_next_message_after_an_answer_goes_back_to_the_assistant() -> None:
    replies = iter([text_result("A KPI is a measurable indicator."), text_result("Sure, glad to help.")])
    fake = FakeBedrockClient(agent_fn=lambda **_: next(replies))
    session_id = str(uuid.uuid4())
    await sb.start_session(session_id, "what is a KPI?", fake)
    turn = await sb.send_message(session_id, "thanks", fake)
    assert turn.assistant_note == "Sure, glad to help." and len(fake.agent_calls) == 2 and fake.calls == []


async def test_web_search_results_go_back_to_the_assistant_as_tool_results() -> None:
    def agent_fn(*, messages, **_):
        if len(messages) == 1:
            return tool_use_result("web_search", {"query": "average call handle time benchmark"})
        result = messages[-1]["content"][0]["toolResult"]
        assert "6 minutes" in result["content"][0]["text"]
        return text_result("Typical handle time is about 6 minutes ([source](https://example.com/aht)).")

    search = FakeWebSearchClient(
        search_fn=lambda q: [search_result("AHT benchmark", "https://example.com/aht", "about 6 minutes")]
    )
    fake = FakeBedrockClient(agent_fn=agent_fn)
    session_id = str(uuid.uuid4())
    turn = await sb.start_session(session_id, "what's a good handle time?", fake, web_search_client=search)
    assert search.queries == ["average call handle time benchmark"]
    assert "6 minutes" in turn.assistant_note and fake.calls == []
    second_call = fake.agent_calls[1]["messages"]
    assert second_call[-2]["role"] == "assistant" and "toolUse" in second_call[-2]["content"][-1]
    assert (await _state(session_id))["web_search_calls_used"] == 1


async def test_the_step_budget_ends_in_an_answer() -> None:
    fake = FakeBedrockClient(agent_fn=lambda **_: tool_use_result("view_kpis", {"names": ["KPI 0"]}))
    session_id = await _seeded()
    turn = await sb.send_message(session_id, "tell me about KPI 0", fake)
    assert len(fake.agent_calls) == agent.MAX_AGENT_STEPS
    assert turn.assistant_note == agent.FALLBACK_ANSWER
    assert "answer now in plain text" in fake.agent_calls[-1]["system"]


# --- hand-off to the builder ----------------------------------------------------------------------------------


async def test_a_build_request_is_handed_to_the_builder_with_the_brief() -> None:
    def agent_fn(**_):
        return tool_use_result("build_scorecard", {"brief": "Call-centre QA scorecard for inbound support calls"})

    pipeline_systems: list[str] = []

    def builder(*, system, **_):
        pipeline_systems.append(system or "")
        return tool_use_result(
            "ask_clarification", {"question": "Who reviews the calls?", "options": ["QA team", "Leads"],
                                  "missing_fields": ["audience"]},
        )

    fake = FakeBedrockClient(agent_fn=agent_fn, converse_fn=builder)
    session_id = str(uuid.uuid4())
    turn = await sb.start_session(session_id, "ok let's do the scorecard we discussed", fake)
    assert turn.status == "awaiting_clarification" and turn.question["question"] == "Who reviews the calls?"
    state = await _state(session_id)
    assert state["build_brief"] == "Call-centre QA scorecard for inbound support calls"
    assert state["research_done"] and state["similarity_checked"]  # the builder ran its once-per-session steps
    # The builder's model sees the brief as an internal note, the user never does.
    assert any("handoff from the chat assistant" in m["content"] for m in state["messages"] if m["role"] == "tool")
    assert "handoff" not in (turn.assistant_note or "")


async def test_a_chip_answer_to_the_builders_question_skips_the_assistant() -> None:
    answers = iter([
        tool_use_result("ask_clarification", {"question": "Who reviews?", "options": ["QA team", "Leads"],
                                              "missing_fields": ["audience"]}),
        tool_use_result("respond_conversationally", {"response": "Noted: the QA team reviews calls."}),
    ])
    fake = FakeBedrockClient(converse_fn=lambda **_: next(answers))
    session_id = str(uuid.uuid4())
    await sb.start_session(session_id, "build a call QA scorecard", fake)
    assert len(fake.agent_calls) == 1  # the default fake hands the first message to the builder
    turn = await sb.send_message(session_id, "QA team", fake)
    assert len(fake.agent_calls) == 1  # the chip answer went straight back to the builder
    assert turn.assistant_note == "Noted: the QA team reviews calls."


async def test_a_typed_answer_to_the_builders_question_is_read_with_the_question_in_view() -> None:
    answers = iter([
        tool_use_result("ask_clarification", {"question": "Who reviews?", "options": ["QA team", "Leads"],
                                              "missing_fields": ["audience"]}),
    ])
    replies = iter([
        tool_use_result("build_scorecard", {"brief": ""}),
        text_result("Good question: leads usually coach, QA scores."),
    ])
    fake = FakeBedrockClient(converse_fn=lambda **_: next(answers), agent_fn=lambda **_: next(replies))
    session_id = str(uuid.uuid4())
    await sb.start_session(session_id, "build a call QA scorecard", fake)
    turn = await sb.send_message(session_id, "what's the difference between the two?", fake)
    assert turn.assistant_note == "Good question: leads usually coach, QA scores."
    assert 'The scorecard builder just asked the user "Who reviews?"' in fake.agent_calls[-1]["system"]


async def test_the_assistants_own_question_comes_back_to_it_even_on_a_chip_click() -> None:
    replies = iter([
        tool_use_result("ask_user", {"question": "Flat or grouped?", "options": ["Flat", "Grouped"]}),
        text_result("Grouped it is: categories keep a big scorecard readable."),
    ])
    fake = FakeBedrockClient(agent_fn=lambda **_: next(replies))
    session_id = await _seeded()
    first = await sb.send_message(session_id, "how should I organise these KPIs?", fake)
    assert first.status == "awaiting_clarification" and first.question["options"] == ["Flat", "Grouped"]
    turn = await sb.send_message(session_id, "Grouped", fake)
    assert len(fake.agent_calls) == 2 and fake.calls == []
    assert turn.assistant_note.startswith("Grouped it is")
    assert 'You just asked the user "Flat or grouped?"' in fake.agent_calls[-1]["system"]


# --- editing, formula and saving ---------------------------------------------------------------------------


async def test_an_edit_is_applied_by_the_builders_update_draft_and_summarised() -> None:
    ops = [{"op": "set_weight", "name": "KPI 0", "weight": 40, "rebalance": "proportional"}]
    fake = FakeBedrockClient(agent_fn=lambda **_: tool_use_result("edit_kpis", {"ops": ops, "assistant_message": ""}))
    session_id = await _seeded()
    turn = await sb.send_message(session_id, "make KPI 0 40% and scale the rest", fake)
    weights = {k["name"]: k["weight"] for k in turn.draft["kpis"]}
    assert weights["KPI 0"] == pytest.approx(40)
    assert _leaf_total(turn.draft) == pytest.approx(100.0, abs=1e-9)
    assert "KPI 0" in turn.assistant_note and "40" in turn.assistant_note  # the server-built before/after summary
    assert fake.calls == []  # no builder model call for a weight change


async def test_an_added_kpi_gets_its_rubric_from_the_builder_model() -> None:
    ops = [{"op": "add", "name": "Empathy", "parent_name": None, "weight": 10, "rationale": "Shows empathy"}]
    fills: list[str | None] = []

    def builder(*, model_id, tools, system, **_):
        fills.append(model_id)
        names = [line.split('"')[1] for line in (system or "").splitlines() if line.startswith('- "')]
        compact = [{"name": n, "suggested_weight": 5, "metric": "m", "unit": "%", "direction": "higher_better",
                    "levels": [f"{n} {i}" for i in range(11)], "thresholds": [i * 10 for i in range(11)]}
                   for n in names]
        return tool_use_result("fill_user_kpi_details", {"kpis": compact})

    fake = FakeOpenRouterClient(
        agent_fn=lambda **_: tool_use_result("edit_kpis", {"ops": ops, "assistant_message": ""}),
        converse_fn=builder,
    )
    session_id = await _seeded()
    turn = await sb.send_message(
        session_id, "add an Empathy KPI at 10%", fake,
        chat_model_id="openai/gpt-5.6-luna", conversation_model_id="anthropic/claude-opus-5.5",
    )
    added = next(k for k in turn.draft["kpis"] if k["name"] == "Empathy")
    assert len(added["guidelines"]) == 11
    assert fills and set(fills) == {"openai/gpt-5.6-luna"}  # rubric writing stays on the builder's model
    assert {c["model_id"] for c in fake.agent_calls} == {"anthropic/claude-opus-5.5"}


async def test_a_valid_formula_is_applied_inline_and_an_invalid_one_is_sent_back() -> None:
    replies = iter([
        tool_use_result("update_scoring_formula", {"formula": 'kpi["Nope"] * 2'}),
        tool_use_result("update_scoring_formula", {"formula": 'min(kpi["KPI 0"], kpi["KPI 1"])'}),
        text_result("Done: the score is now the lower of KPI 0 and KPI 1."),
    ])
    seen: list[str] = []

    def agent_fn(*, messages, **_):
        last = messages[-1]["content"][0]
        if "toolResult" in last:
            seen.append(last["toolResult"]["content"][0]["text"])
        return next(replies)

    fake = FakeBedrockClient(agent_fn=agent_fn)
    session_id = await _seeded()
    turn = await sb.send_message(session_id, "score it as the minimum of KPI 0 and KPI 1", fake)
    assert seen[0].startswith("REJECTED") and seen[1].startswith("Scoring formula set")
    assert turn.draft["scoring_formula"] == 'min(kpi["KPI 0"], kpi["KPI 1"])'
    assert turn.assistant_note.startswith("Done")


async def test_save_confirms_a_complete_draft() -> None:
    fake = FakeBedrockClient(
        agent_fn=lambda **_: tool_use_result("save_scorecard", {"assistant_message": "Saving it now."})
    )
    session_id = await _seeded()
    turn = await sb.send_message(session_id, "looks good, save it", fake)
    assert turn.status == "confirmed" and fake.calls == []


async def test_draft_tools_are_offered_only_when_there_is_a_draft() -> None:
    fake = FakeBedrockClient(agent_fn=lambda **_: text_result("Hi."))
    await sb.start_session(str(uuid.uuid4()), "hello", fake)
    offered = {t.name for t in fake.agent_calls[0]["tools"]}
    assert offered == {"build_scorecard", "ask_user"}
    session_id = await _seeded()
    await sb.send_message(session_id, "hi again", fake, web_search_client=FakeWebSearchClient())
    offered = {t.name for t in fake.agent_calls[1]["tools"]}
    assert offered == {"build_scorecard", "ask_user", "view_kpis", "edit_kpis", "update_scoring_formula",
                       "save_scorecard", "web_search"}
    assert "KPI 0 (25%)" in fake.agent_calls[1]["system"]


# --- provider details ------------------------------------------------------------------------------------------


async def test_openrouter_calls_carry_reasoning_session_and_cache_point() -> None:
    fake = FakeOpenRouterClient(agent_fn=lambda **_: text_result("Hi."))
    session_id = str(uuid.uuid4())
    await sb.start_session(session_id, "hello", fake, conversation_model_id="anthropic/claude-sonnet-5.5")
    call = fake.agent_calls[0]
    assert call["model_id"] == "anthropic/claude-sonnet-5.5"
    assert call["keep_reasoning"] is True and call["session_key"] == session_id
    assert call["reasoning_effort"] == "medium"
    assert call["messages"][-1]["content"][-1] == {"cachePoint": {"type": "default"}}


async def test_bedrock_calls_carry_no_openrouter_extras() -> None:
    fake = FakeBedrockClient(agent_fn=lambda **_: text_result("Hi."))
    await sb.start_session(str(uuid.uuid4()), "hello", fake)
    call = fake.agent_calls[0]
    assert call["keep_reasoning"] is False and call["session_key"] is None and call["reasoning_effort"] is None
    assert all("cachePoint" not in block for block in call["messages"][-1]["content"])


async def test_several_lookups_in_one_response_are_all_answered_and_reasoning_is_passed_back() -> None:
    def agent_fn(*, messages, **_):
        if "toolResult" not in messages[-1]["content"][0]:
            return ConverseResult(
                stop_reason="tool_use", tool_name="view_kpis", tool_input={"names": ["KPI 0"]}, tool_use_id="a",
                tool_calls=[ToolCall("a", "view_kpis", {"names": ["KPI 0"]}),
                            ToolCall("b", "view_kpis", {"names": ["KPI 1", "Missing"]})],
                reasoning_details=[{"type": "reasoning.encrypted", "data": "x"}],
            )
        results = [b["toolResult"] for b in messages[-1]["content"]]
        assert [r["toolUseId"] for r in results] == ["a", "b"]
        assert "Not in the draft: Missing" in results[1]["content"][0]["text"]
        assert {"reasoningDetails": [{"type": "reasoning.encrypted", "data": "x"}]} in messages[-2]["content"]
        return text_result("KPI 0 and KPI 1 are both weighted 25%.")

    fake = FakeBedrockClient(agent_fn=agent_fn)
    session_id = await _seeded()
    turn = await sb.send_message(session_id, "compare KPI 0 and KPI 1", fake)
    assert turn.assistant_note == "KPI 0 and KPI 1 are both weighted 25%."


# --- kill switch ---------------------------------------------------------------------------------------------


def test_without_the_assistant_the_graph_has_the_original_wiring() -> None:
    nodes = set(sb.build_graph_definition(chat_agent=False).nodes)
    assert "converse" not in nodes
    assert "converse" in set(sb.build_graph_definition(chat_agent=True).nodes)


def test_draft_outline_lists_categories_weights_and_completeness() -> None:
    draft = ScorecardDraft.model_validate(
        {
            "name": "QA", "kpis": [
                {"name": "Quality", "level": 1},
                {"name": "Accuracy", "level": 2, "parent_name": "Quality", "weight": 60},
                {"name": "Tone", "level": 2, "parent_name": "Quality", "weight": 40, "included_in_scoring": False},
            ],
        }
    )
    outline = agent.draft_outline(draft)
    assert "- Quality (category)" in outline
    assert "  - Accuracy (60%)" in outline and "  - Tone (40%, excluded from scoring)" in outline
    assert "Still missing:" in outline
    assert agent.draft_outline(ScorecardDraft()) == "(nothing drafted yet in this chat)"
