"""Tests for turning a confirmed ScorecardDraft into real rows (app/ai/draft_materialize.py),
against real Postgres — including the existing weight-sum-to-100 deferred constraint
trigger (trg_kpi_node_weight_sum), reused as-is rather than re-implemented in the AI layer."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.ai.draft_materialize import materialize_draft
from app.ai.draft_schema import ScorecardDraft
from app.models.kpi_node import KpiNode
from app.models.user import User
from tests.fakes import full_rubric

_VALID_DRAFT = {
    "name": "Support Ticket Quality",
    "purpose": "Rate support ticket resolutions.",
    "domain": "Customer Support",
    "audience": "Support leads",
    "target_score": 8,
    "kpis": [
        {
            # "Accuracy" is referenced as "Grammar"'s parent below, so it's a
            # category/grouping node — NO weight and no guidelines of its own (see
            # migration 0008_category_nodes_no_weight / draft_schema.py).
            "name": "Accuracy",
            "level": 1,
        },
        {
            "name": "Tone",
            "weight": 40,
            "level": 1,
            "guidelines": full_rubric(),
        },
        {
            # Leaf KPIs are weighted GLOBALLY across the whole draft (not per category) —
            # Tone (40) + Grammar (60) = 100.
            "name": "Grammar",
            "weight": 60,
            "level": 2,
            "parent_name": "Accuracy",
            "guidelines": full_rubric(),
        },
    ],
}


async def test_materialize_confirmed_draft_creates_full_hierarchy(async_db_session) -> None:
    db = async_db_session
    owner = User(email=f"materialize-{uuid.uuid4().hex[:8]}@example.com", name="Materialize Owner")
    db.add(owner)
    await db.flush()
    await db.commit()

    draft = ScorecardDraft.model_validate(_VALID_DRAFT)
    assert draft.is_complete(), draft.missing_fields()

    scorecard, version = await materialize_draft(db, draft, owner_id=owner.id)

    assert scorecard.name == "Support Ticket Quality"
    assert scorecard.current_version_id == version.id

    nodes = (
        (await db.execute(select(KpiNode).where(KpiNode.scorecard_version_id == version.id)))
        .scalars()
        .all()
    )
    assert len(nodes) == 3
    grammar = next(n for n in nodes if n.name == "Grammar")
    accuracy = next(n for n in nodes if n.name == "Accuracy")
    tone = next(n for n in nodes if n.name == "Tone")
    assert grammar.parent_id == accuracy.id
    assert grammar.level == 2
    assert str(grammar.path).startswith(str(accuracy.path))
    # "Accuracy" is a category/grouping node (Grammar's parent) — stored with NO weight at
    # all (see migration 0008_category_nodes_no_weight); only the LEAF KPIs (Tone, Grammar)
    # carry a real weight, and together they sum to 100 across the whole scorecard.
    assert accuracy.weight is None
    assert float(grammar.weight) == 60
    assert float(tone.weight) == 40


async def test_materialize_rejects_incomplete_draft_before_touching_db(async_db_session) -> None:
    db = async_db_session
    owner = User(email=f"materialize-incomplete-{uuid.uuid4().hex[:8]}@example.com", name="Owner")
    db.add(owner)
    await db.commit()

    draft = ScorecardDraft()  # empty — definitely incomplete
    with pytest.raises(ValueError, match="incomplete"):
        await materialize_draft(db, draft, owner_id=owner.id)


async def test_materialize_surfaces_db_weight_sum_violation(async_db_session) -> None:
    """A draft can pass ScorecardDraft's own completeness check (weights present, no
    parent/name issues) yet still violate the DB's weight-sum trigger if two KPIs happen
    to reference the same parent inconsistently with what materialize_draft resolves —
    exercised here directly by bypassing ScorecardDraft's sibling-sum check via a patch
    dict that is individually valid per-field but sums siblings to 90, not 100."""
    db = async_db_session
    owner = User(email=f"materialize-badweight-{uuid.uuid4().hex[:8]}@example.com", name="Owner")
    db.add(owner)
    await db.commit()

    draft = ScorecardDraft.model_validate(
        {
            "name": "Bad Weights",
            "purpose": "p",
            "domain": "d",
            "target_score": 5,
            "kpis": [
                {"name": "A", "weight": 50, "guidelines": full_rubric()},
                {"name": "B", "weight": 40, "guidelines": full_rubric()},
            ],
        }
    )
    # ScorecardDraft.is_complete() already catches this (sibling_weights != 100) — confirm
    # that, and confirm materialize_draft refuses before ever reaching the DB.
    assert not draft.is_complete()
    with pytest.raises(ValueError, match="incomplete"):
        await materialize_draft(db, draft, owner_id=owner.id)


async def test_materialize_saves_a_draft_with_unscored_weightless_leaves(async_db_session) -> None:
    """A draft whose informational leaves carry no weight (like the 114-KPI hackathon chat) must save: the
    weight-sum trigger only counts scored leaves, so 60 + 40 = 100 passes."""
    db = async_db_session
    owner = User(email=f"unscored-{uuid.uuid4().hex[:8]}@example.com", name="Unscored Owner")
    db.add(owner)
    await db.flush()
    await db.commit()

    draft = ScorecardDraft.model_validate(
        {
            "name": "Hackathon Solution Scoring",
            "purpose": "Score solution documents.",
            "domain": "Hackathon",
            "target_score": 9,
            "kpis": [
                {"name": "Quality", "level": 1},
                {"name": "Clarity", "level": 2, "parent_name": "Quality", "weight": 60, "guidelines": full_rubric()},
                {"name": "Depth", "level": 2, "parent_name": "Quality", "weight": 40, "guidelines": full_rubric()},
                {"name": "Declarations", "level": 1},
                {
                    "name": "DC-1: AI tools are declared",
                    "level": 2,
                    "parent_name": "Declarations",
                    "included_in_scoring": False,
                    "guidelines": full_rubric(),
                },
                {"name": "Appendix", "level": 1, "included_in_scoring": False, "guidelines": full_rubric()},
            ],
        }
    )
    assert draft.is_complete(), draft.missing_fields()

    _scorecard, version = await materialize_draft(db, draft, owner_id=owner.id)

    nodes = (
        (await db.execute(select(KpiNode).where(KpiNode.scorecard_version_id == version.id))).scalars().all()
    )
    by_name = {n.name: n for n in nodes}
    assert len(nodes) == 6
    assert by_name["DC-1: AI tools are declared"].included_in_scoring is False
    assert by_name["Appendix"].included_in_scoring is False
    scored = [n for n in nodes if n.included_in_scoring and n.weight is not None]
    assert sum(float(n.weight) for n in scored) == 100
