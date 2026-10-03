"""Pure Pydantic validation tests for `ScorecardDraft` (app/ai/draft_schema.py) — no DB,
no Bedrock. These are the rules `update_draft` patches are checked against before ever
being committed to LangGraph state (see test_scorecard_builder.py for that integration)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.draft_schema import ScorecardDraft
from tests.fakes import full_rubric


def test_empty_draft_is_incomplete() -> None:
    draft = ScorecardDraft()
    assert not draft.is_complete()
    missing = draft.missing_fields()
    assert "purpose" in missing
    assert "domain" in missing
    assert "kpis" in missing


def test_complete_draft_reports_no_missing_fields() -> None:
    draft = ScorecardDraft.model_validate(
        {
            "name": "Support Ticket Quality",
            "purpose": "Rate the quality of support ticket resolutions.",
            "domain": "Customer Support",
            "audience": "Support team leads",
            "target_score": 8,
            "kpis": [
                {
                    "name": "Accuracy",
                    "weight": 60,
                    "level": 1,
                    "guidelines": full_rubric(),
                },
                {
                    "name": "Tone",
                    "weight": 40,
                    "level": 1,
                    "guidelines": full_rubric(),
                },
            ],
        }
    )
    assert draft.is_complete(), draft.missing_fields()


def test_sibling_weights_not_summing_to_100_is_reported_as_missing() -> None:
    draft = ScorecardDraft.model_validate(
        {
            "purpose": "p",
            "domain": "d",
            "name": "n",
            "target_score": 5,
            "kpis": [
                {"name": "A", "weight": 60, "guidelines": full_rubric()},
                {"name": "B", "weight": 30, "guidelines": full_rubric()},
            ],
        }
    )
    missing = draft.missing_fields()
    assert any("sibling_weights" in m for m in missing)


def test_nested_hierarchy_sibling_weights_checked_per_parent() -> None:
    """A's children (B, C) must independently sum to 100, separately from the root group."""
    draft = ScorecardDraft.model_validate(
        {
            "purpose": "p",
            "domain": "d",
            "name": "n",
            "target_score": 5,
            "kpis": [
                {"name": "A", "weight": 100, "level": 1, "guidelines": full_rubric()},
                {
                    "name": "B",
                    "weight": 50,
                    "level": 2,
                    "parent_name": "A",
                    "guidelines": full_rubric(),
                },
                {
                    "name": "C",
                    "weight": 50,
                    "level": 2,
                    "parent_name": "A",
                    "guidelines": full_rubric(),
                },
            ],
        }
    )
    assert draft.is_complete(), draft.missing_fields()


def test_parent_name_must_reference_an_existing_kpi_name() -> None:
    with pytest.raises(ValidationError):
        ScorecardDraft.model_validate(
            {"kpis": [{"name": "Orphan", "parent_name": "DoesNotExist"}]}
        )


def test_kpi_cannot_be_its_own_parent() -> None:
    with pytest.raises(ValidationError):
        ScorecardDraft.model_validate({"kpis": [{"name": "Self", "parent_name": "Self"}]})


def test_guideline_score_level_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ScorecardDraft.model_validate(
            {"kpis": [{"name": "A", "guidelines": {"11": {"qualitative_text": "x"}}}]}
        )


def test_guideline_score_level_not_an_integer_rejected() -> None:
    with pytest.raises(ValidationError):
        ScorecardDraft.model_validate(
            {"kpis": [{"name": "A", "guidelines": {"high": {"qualitative_text": "x"}}}]}
        )


def test_weight_out_of_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        ScorecardDraft.model_validate({"kpis": [{"name": "A", "weight": 150}]})


def test_hierarchy_level_out_of_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        ScorecardDraft.model_validate({"kpis": [{"name": "A", "level": 5}]})
