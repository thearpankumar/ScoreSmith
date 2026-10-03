"""User-specified/hybrid first turn: when preparation already produced a complete draft, the model visit is
skipped; the visible text is side-answer + server summary + ONE question, with the standard option chips.
Also: the '[system note]' prefix never leaks into a visible/persisted message."""

from __future__ import annotations

import uuid

import pytest

import app.ai.scorecard_builder as sb
from app.ai.draft_schema import ScorecardDraft
from app.schemas.chat import ChatTurnRead
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, tool_use_result
from tests.test_scorecard_builder_user_specified import _USER_MESSAGE, _classify, _Pipeline

pytestmark = pytest.mark.usefixtures("_migrated_db")


async def test_complete_draft_after_preparation_makes_zero_propose_model_calls() -> None:
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify())
    turn = await sb.start_session(
        str(uuid.uuid4()), _USER_MESSAGE, fake, web_search_client=FakeWebSearchClient(search_fn=lambda q: [])
    )
    assert pipeline.propose_systems == []  # no propose_kpis model visit at all
    assert turn.status == "awaiting_clarification"
    assert turn.question["question"] == sb.FIRST_TURN_QUESTION
    assert turn.question["options"] == sb.FIRST_TURN_OPTIONS  # chips for the UI card
    assert ScorecardDraft.model_validate(turn.draft).is_complete()
    note = turn.assistant_note
    assert "names kept exactly" in note and "Filled in:" in note
    assert "?" not in note  # the question lives ONLY in the card
    # the UI contract: ChatTurnRead.question {question, options, missing_fields}
    read = ChatTurnRead(
        session_id=uuid.uuid4(), status=turn.status, draft=turn.draft,
        question={"question": turn.question["question"], "options": turn.question["options"], "missing_fields": []},
        assistant_message=note,
    )
    assert read.question.options == sb.FIRST_TURN_OPTIONS


async def test_side_question_answer_summary_and_one_question_are_all_visible() -> None:
    def side(**_kw):
        return tool_use_result("answer_user_questions", {"answer": "It standardises how PR quality is judged."})

    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify(), side_fn=side)
    turn = await sb.start_session(
        str(uuid.uuid4()), _USER_MESSAGE + " Also, why is this scorecard needed in the first place?", fake,
        web_search_client=FakeWebSearchClient(search_fn=lambda q: []),
    )
    assert pipeline.propose_systems == []
    note = turn.assistant_note
    assert note.startswith("It standardises how PR quality is judged.")
    assert "names kept exactly" in note and "?" not in note and turn.question["question"]


async def test_incomplete_draft_still_gets_a_model_visit(monkeypatch) -> None:
    visits: list[int] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        visits.append(1)
        return tool_use_result("ask_clarification", {"question": "Which?", "options": [], "missing_fields": []})

    state = {
        "session_id": str(uuid.uuid4()),
        "draft": ScorecardDraft().model_dump(mode="json"),  # nothing filled => not complete
        "messages": [{"role": "user", "content": "x"}],
        "llm_turn_count": 0,
        "user_spec": {"mode": "user_specified", "pinned": [{"name": "A"}], "notes": []},
    }
    from tests.fakes import FakeJevClient

    cfg = {"configurable": {"bedrock_client": FakeBedrockClient(converse_fn=fn), "jev_client": FakeJevClient()}}
    out = await sb.propose_kpis(state, cfg)
    assert visits and out["pending_tool"]["name"] == "ask_clarification"


# --- '[system note]' never leaks --------------------------------------------------------------------------


def test_system_note_prefix_is_stripped_from_visible_notes_and_not_added_to_server_summaries() -> None:
    messages = [
        {"role": "user", "content": "Make X 8%"},
        {"role": "assistant", "content": "[system note] I applied your change.\n- X: 5.70% → 8.00%"},
    ]
    assert sb._visible_turn_notes(messages) == "I applied your change.\n- X: 5.70% → 8.00%"
    folded = sb._messages_to_converse(
        [
            {"role": "user", "content": "u"},
            {"role": "tool", "content": "I applied your change.", "kind": "edit_summary"},
        ]
    )
    assert folded[-1]["content"][0]["text"] == "I applied your change."
    other = sb._messages_to_converse([{"role": "user", "content": "u"}, {"role": "tool", "content": "note"}])
    assert other[-1]["content"][0]["text"] == "[system note] note"  # other tool notes keep the tag
    assert sb._strip_system_prefix("[system note] hello") == "hello" and sb._strip_system_prefix("hello") == "hello"


async def test_model_echoing_the_prefix_does_not_reach_the_pending_note() -> None:
    from tests.fakes import FakeJevClient

    def fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result(
            "respond_conversationally", {"response": "[system note] I applied your change. Anything else?"}
        )

    state = {
        "session_id": str(uuid.uuid4()),
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": []},
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"},
                     {"role": "user", "content": "thanks, that is all"}],
        "llm_turn_count": 0,
    }
    cfg = {"configurable": {"bedrock_client": FakeBedrockClient(converse_fn=fn), "jev_client": FakeJevClient()}}
    out = await sb.propose_kpis(state, cfg)
    assert out["pending_tool"]["input"]["response"] == "I applied your change. Anything else?"
    assert out["messages"][-1]["content"] == "I applied your change. Anything else?"


# --- the question card is the single place the question appears ----------------------------------------------


def _interrupt(value: dict):
    from types import SimpleNamespace

    return [SimpleNamespace(value=value)]


def test_turn_with_a_question_card_keeps_the_question_out_of_the_message_text() -> None:
    q = "Would you like to save this scorecard as-is, or adjust something first?"
    state = {
        "__interrupt__": _interrupt({"question": q, "options": ["Save it as-is"], "missing_fields": []}),
        "draft": {},
        "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "It standardises reviews."},
            {"role": "assistant", "content": "I built the scorecard from your 35 KPIs.\n- Name: X"},
            {"role": "assistant",
             "content": "Would you like to save this scorecard as-is, or adjust anything (weights, target score)?"},
            {"role": "assistant", "content": q},
        ],
    }
    result = sb._to_turn_result(state)
    assert result.question["question"] == q
    assert "?" not in result.assistant_note
    assert result.assistant_note == "It standardises reviews.\n\nI built the scorecard from your 35 KPIs.\n- Name: X"


def test_plain_clarifying_question_with_nothing_else_keeps_its_text() -> None:
    state = {
        "__interrupt__": _interrupt({"question": "What is the purpose?", "options": [], "missing_fields": []}),
        "draft": {},
        "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "What is the purpose?"}],
    }
    assert sb._to_turn_result(state).assistant_note == "What is the purpose?"


def test_turn_without_a_card_keeps_exactly_one_closing_question_in_the_text() -> None:
    summary = (
        "I applied your change.\n- X: 5.70% → 8.00%\n\n"
        "Would you like to save it as it is now, or change anything else?"
    )
    state = {
        "__interrupt__": _interrupt({"kind": "conversational_response", "response": summary}),
        "draft": {},
        "messages": [
            {"role": "user", "content": "x"},
            {"role": "tool", "content": summary, "kind": "edit_summary"},
        ],
    }
    result = sb._to_turn_result(state)
    assert result.question is None and result.assistant_note.count("?") == 1


async def test_persisted_first_turn_message_has_no_question_when_a_card_exists() -> None:
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify())
    turn = await sb.start_session(
        str(uuid.uuid4()), _USER_MESSAGE, fake, web_search_client=FakeWebSearchClient(search_fn=lambda q: [])
    )
    assert turn.question["question"] == sb.FIRST_TURN_QUESTION
    assert "?" not in turn.assistant_note and "names kept exactly" in turn.assistant_note


# --- forced pause (turn-visit cap) -------------------------------------------------------------------------


async def test_forced_pause_shows_a_server_summary_and_one_card_question_never_the_generic_line() -> None:
    from tests.fakes import FakeJevClient, full_rubric

    kpis = [
        {"name": "A", "weight": 60, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric()},
        {"name": "B", "weight": 40, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric()},
    ]
    state = {
        "session_id": str(uuid.uuid4()),
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": kpis},
        "messages": [{"role": "user", "content": "tweak things"}],
        "llm_turn_count": sb.MAX_LLM_TURNS_PER_HUMAN_TURN,
        "turn_base_weights": {"A": 50, "B": 50},
    }
    cfg = {"configurable": {"bedrock_client": FakeBedrockClient(), "jev_client": FakeJevClient()}}
    node = await sb.propose_kpis(state, cfg)
    pause_text = "I've made several updates"
    assert all(pause_text not in m["content"] for m in node["messages"])
    assert node["pending_tool"]["name"] == "ask_clarification"
    assert node["pending_tool"]["input"]["options"] == sb.FIRST_TURN_OPTIONS
    result = sb._to_turn_result(
        {
            "__interrupt__": _interrupt(node["pending_tool"]["input"]),
            "draft": state["draft"],
            "messages": state["messages"] + node["messages"],
        }
    )
    assert result.status == "awaiting_clarification" and result.question["question"] == sb.FIRST_TURN_QUESTION
    assert "?" not in result.assistant_note  # exactly one question: the card
    assert "2 KPIs" in result.assistant_note and "Changed this turn: 2 weight(s)" in result.assistant_note


def test_first_turn_summary_is_compact_with_35_weightless_kpis() -> None:
    from tests.fakes import full_rubric

    n = 35
    names = [f"Signal Metric {chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(n)]
    weights = [round(100 / n + (n / 2 - i) * 0.05, 2) for i in range(n)]
    weights[0] = round(weights[0] + 100 - sum(weights), 2)
    kpis = [
        {"name": nm, "weight": w, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric(nm)}
        for nm, w in zip(names, weights, strict=True)
    ]
    draft = ScorecardDraft.model_validate(
        {"name": "Support Quality", "purpose": "p", "domain": "d", "audience": "a", "target_score": 8, "kpis": kpis}
    )
    huge_notes = [
        f"You didn't give weights for: {'; '.join(names)}",
        "I proposed weights for " + ", ".join(names) + ": " + "; ".join(f"{x} - because reasons" for x in names),
    ]
    spec = {"pinned": [{"name": x, "weight": None} for x in names], "notes": huge_notes}
    text = sb._first_turn_summary(draft, spec)
    assert len(text) <= sb.SUMMARY_MAX_CHARS
    assert sum(1 for x in names if x in text) <= 8  # top 5 + bottom 3 at most
    top = sorted(kpis, key=lambda k: -k["weight"])[:5]
    for k in top:
        assert f"{k['name']} ({k['weight']:.1f}%)" in text  # numbers are the saved weights
    assert "none were given" in text and "because reasons" not in text and "?" not in text


def test_forced_pause_and_edit_summaries_are_capped() -> None:
    long = "x" * 3000
    assert len(sb._cap_summary(long + "\n" + long)) <= sb.SUMMARY_MAX_CHARS + 50
    kpis = [
        {"name": f"K{i}", "weight": 1.0, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": {}} for i in range(40)
    ]
    new = [{**k, "weight": 2.0} for k in kpis]
    summary = sb._edit_summary(kpis, new, set(), None)
    assert len(summary) <= sb.SUMMARY_MAX_CHARS + 50 and summary.count("K") < 30
