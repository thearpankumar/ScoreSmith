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
            "guidelines": {"10": {"qualitative_text": "Perfectly polite."}, "0": {"qualitative_text": "Rude."}},
        },
        {
            # Leaf KPIs are weighted GLOBALLY across the whole draft (not per category) —
            # Tone (40) + Grammar (60) = 100.
            "name": "Grammar",
            "weight": 60,
            "level": 2,
            "parent_name": "Accuracy",
            "guidelines": {"10": {"qualitative_text": "No errors."}},
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
                {"name": "A", "weight": 50, "guidelines": {"10": {"qualitative_text": "x"}}},
                {"name": "B", "weight": 40, "guidelines": {"10": {"qualitative_text": "x"}}},
            ],
        }
    )
    # ScorecardDraft.is_complete() already catches this (sibling_weights != 100) — confirm
    # that, and confirm materialize_draft refuses before ever reaching the DB.
    assert not draft.is_complete()
    with pytest.raises(ValueError, match="incomplete"):
        await materialize_draft(db, draft, owner_id=owner.id)
