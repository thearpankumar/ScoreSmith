"""Final polish of the edit reply: ONLY the server-built summary is visible (real old -> new values, rescale
note, real total, ONE closing question); the model's remembered numbers and duplicate questions never are."""

from __future__ import annotations

import app.ai.scorecard_builder as sb
from tests.fakes import full_rubric

MODEL_PARAGRAPH = (
    "I've set Customer Satisfaction Score to 8% and scaled the other KPIs proportionally. The highest-weighted "
    "KPIs now are: Customer Satisfaction Score (8%), Compliance Violation Count (5.83%), Data Privacy Incident "
    "Rate (4.88%), and Customer Churn After Contact (4.39%). Would you like to save this scorecard as-is, or "
    "adjust anything else?"
)


def _kpis() -> list[dict]:
    weights = {"Customer Satisfaction Score": 8.0, "Compliance Violation Count": 5.85,
               "Data Privacy Incident Rate": 4.90, "Customer Churn After Contact": 4.41}
    rest = round(100 - sum(weights.values()), 2)
    out = [
        {"name": n, "weight": w, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": full_rubric(n)}
        for n, w in weights.items()
    ]
    out.append({"name": "Everything Else", "weight": rest, "level": 1, "parent_name": None,
                "included_in_scoring": True, "guidelines": full_rubric("e")})
    return out


def test_model_paragraph_with_wrong_numbers_and_its_question_never_reach_the_visible_text() -> None:
    """Exact live scenario: update_draft prose (wrong numbers + question) first, then the server summary."""
    summary = sb._edit_summary(
        {"Customer Satisfaction Score": 2.86, "Compliance Violation Count": 5.92,
         "Data Privacy Incident Rate": 4.96, "Customer Churn After Contact": 4.46},
        _kpis(), {"Customer Satisfaction Score"}, MODEL_PARAGRAPH,
    )
    messages = [
        {"role": "user", "content": "Make Customer Satisfaction Score 8% and scale the others down proportionally"},
        {"role": "assistant", "content": MODEL_PARAGRAPH},
        {"role": "tool", "content": MODEL_PARAGRAPH},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": summary, "kind": "edit_summary"},
    ]
    visible = sb._visible_turn_notes(messages)
    assert visible == summary
    assert "5.83" not in visible and "4.88" not in visible and "4.39" not in visible
    assert "Customer Satisfaction Score: 2.86% → 8.00%" in visible  # the directly edited KPI, real old -> new
    assert "rescaled" in visible and "Total weight of the scored KPIs: 100.00%" in visible
    assert visible.count("?") == 1  # one closing question
    assert "highest-weighted" not in visible


def test_edit_summary_baseline_is_the_turn_start_even_if_the_change_was_already_applied() -> None:
    """The model applied the change via update_draft first; the edit_kpis step then changes nothing, but the
    summary still shows the real turn-level old -> new."""
    base = {"Customer Satisfaction Score": 2.86, "Compliance Violation Count": 5.92,
            "Data Privacy Incident Rate": 4.96, "Customer Churn After Contact": 4.46, "Everything Else": 81.8}
    state = {
        "draft": {"name": "n", "purpose": "p", "domain": "d", "target_score": 8, "kpis": _kpis()},
        "messages": [{"role": "user", "content": "Make Customer Satisfaction Score 8% and scale the others down"}],
        "pending_tool": {"name": "edit_kpis", "input": {
            "ops": [{"op": "set_weight", "name": "Customer Satisfaction Score", "weight": 8}],
            "assistant_message": MODEL_PARAGRAPH}},
        "turn_base_weights": base,
        "user_spec": None,
    }
    out = sb.update_draft(state)
    note = out["messages"][0]["content"]
    assert out["messages"][0]["kind"] == "edit_summary"
    assert "Customer Satisfaction Score: 2.86% → 8.00%" in note
    assert "5.83" not in note and note.count("?") == 1


def test_model_explanation_keeps_only_number_free_non_question_sentences() -> None:
    assert sb._clean_explanation(MODEL_PARAGRAPH) is None
    assert sb._clean_explanation("Scaled everything proportionally. Want to save it?") == (
        "Scaled everything proportionally."
    )


def test_semantically_duplicate_closing_questions_keep_only_the_last() -> None:
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "Here is the answer to your question. Shall I save it as-is?"},
        {"role": "assistant", "content": "Would you like to save this scorecard, or adjust anything else?"},
    ]
    visible = sb._visible_turn_notes(messages)
    assert visible == (
        "Here is the answer to your question.\n\nWould you like to save this scorecard, or adjust anything else?"
    )
    assert visible.count("?") == 1


def test_user_specified_first_turn_is_answer_plus_one_question() -> None:
    messages = [
        {"role": "user", "content": "KPIs ... Also, why is this scorecard needed?"},
        {"role": "assistant", "content": "[research] internal"},
        {"role": "assistant", "content": "It standardises reviews so quality is comparable across teams."},
        {"role": "tool", "content": "I filled the weights and 0-10 guidelines. Do you want to save it as-is?"},
        {"role": "assistant", "content": "Would you like to save this scorecard as-is, or adjust anything?"},
    ]
    visible = sb._visible_turn_notes(messages)
    assert visible.count("?") == 1 and visible.startswith("It standardises reviews")
    assert visible.endswith("Would you like to save this scorecard as-is, or adjust anything?")
