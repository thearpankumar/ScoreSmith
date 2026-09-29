"""Tests for the DB-level weight-sum-to-100-per-sibling-group deferred constraint trigger
(`trg_kpi_node_weight_sum`, defined in alembic/versions/0001_initial_schema.py).

The trigger is a DEFERRED CONSTRAINT TRIGGER, so it only evaluates at COMMIT by default;
`SET CONSTRAINTS trg_kpi_node_weight_sum IMMEDIATE` is used to force it to fire
synchronously within a test so the exception can be asserted directly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User


def _make_scorecard_version(db_session: Session) -> ScorecardVersion:
    owner = User(email=f"trigger-{uuid.uuid4().hex[:8]}@example.com", name="Trigger Test Owner")
    db_session.add(owner)
    db_session.flush()
    scorecard = Scorecard(name="Trigger Test Scorecard", owner_id=owner.id)
    db_session.add(scorecard)
    db_session.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db_session.add(version)
    db_session.flush()
    return version


def _node(version: ScorecardVersion, name: str, weight: float, display_order: int = 0) -> KpiNode:
    node_id = uuid.uuid4()
    return KpiNode(
        id=node_id,
        scorecard_version_id=version.id,
        parent_id=None,
        path=Ltree(node_id.hex),
        level=1,
        name=name,
        weight=weight,
        display_order=display_order,
    )


def test_sibling_weights_summing_to_100_commits_successfully(db_session: Session) -> None:
    version = _make_scorecard_version(db_session)
    db_session.add_all(
        [
            _node(version, "KPI A", 60, 0),
            _node(version, "KPI B", 40, 1),
        ]
    )
    # Should not raise: the deferred trigger fires at commit and the sum is exactly 100.
    db_session.commit()

    nodes = db_session.query(KpiNode).filter_by(scorecard_version_id=version.id).all()
    assert len(nodes) == 2
    assert sum(float(n.weight) for n in nodes) == 100


def test_sibling_weights_summing_to_97_is_rejected(db_session: Session) -> None:
    version = _make_scorecard_version(db_session)
    db_session.add_all(
        [
            _node(version, "KPI A", 57, 0),
            _node(version, "KPI B", 40, 1),
        ]
    )
    db_session.flush()
    # Force the deferred trigger to evaluate now instead of waiting for an eventual commit.
    with pytest.raises(IntegrityError, match="weight sum"):
        db_session.execute(text("SET CONSTRAINTS trg_kpi_node_weight_sum IMMEDIATE"))
    db_session.rollback()


def test_sibling_weights_summing_to_103_is_rejected(db_session: Session) -> None:
    version = _make_scorecard_version(db_session)
    db_session.add_all(
        [
            _node(version, "KPI A", 63, 0),
            _node(version, "KPI B", 40, 1),
        ]
    )
    db_session.flush()
    with pytest.raises(IntegrityError, match="weight sum"):
        db_session.execute(text("SET CONSTRAINTS trg_kpi_node_weight_sum IMMEDIATE"))
    db_session.rollback()


def test_root_siblings_are_scoped_per_scorecard_version(db_session: Session) -> None:
    """Two different scorecard versions can each have a single root KPI at 100% weight —
    their NULL parent_id must not be treated as one shared sibling group."""
    version_a = _make_scorecard_version(db_session)
    version_b = _make_scorecard_version(db_session)
    db_session.add(_node(version_a, "Only KPI (A)", 100, 0))
    db_session.add(_node(version_b, "Only KPI (B)", 100, 0))
    db_session.commit()  # must not raise

    assert db_session.query(KpiNode).filter_by(scorecard_version_id=version_a.id).count() == 1
    assert db_session.query(KpiNode).filter_by(scorecard_version_id=version_b.id).count() == 1


def test_deleting_last_sibling_does_not_violate_trigger(db_session: Session) -> None:
    version = _make_scorecard_version(db_session)
    node = _node(version, "Only KPI", 100, 0)
    db_session.add(node)
    db_session.commit()

    db_session.delete(node)
    db_session.commit()  # must not raise — an empty sibling group has nothing to sum

    assert db_session.query(KpiNode).filter_by(scorecard_version_id=version.id).count() == 0
