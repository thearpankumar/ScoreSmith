"""Tests exercising the scenario-based dataset generator (app/scripts/generate_scenarios.py)
directly, covering multiple categories from the plan's Cycle 1 scenario catalogue:
normal/happy-path, boundary cases, invalid/flawed data, and migration cases.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.evaluation import Evaluation
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.scripts.generate_scenarios import (
    ScenarioReport,
    get_or_create_user,
    scenario_boundary_cases,
    scenario_embedding_model_backfill,
    scenario_flat_6kpi,
    scenario_invalid_duplicate_names,
    scenario_invalid_weight_sum,
    scenario_mid_band_and_double_evaluation,
    scenario_version_bump_preserves_evaluations,
)


def _owner(db_session: Session):
    owner = get_or_create_user(db_session, "scenario-owner@example.com", "Scenario Owner", "designer")
    db_session.commit()
    return owner


# --- normal / happy-path ---


def test_scenario_flat_6kpi_and_double_evaluation(db_session: Session) -> None:
    owner = _owner(db_session)
    evaluator_a = get_or_create_user(db_session, "eval-a@example.com", "Evaluator A", "evaluator")
    evaluator_b = get_or_create_user(db_session, "eval-b@example.com", "Evaluator B", "evaluator")
    db_session.commit()

    report = ScenarioReport()
    version = scenario_flat_6kpi(db_session, report, owner)
    scenario_mid_band_and_double_evaluation(db_session, report, version, evaluator_a, evaluator_b)
    db_session.commit()

    nodes = db_session.query(KpiNode).filter_by(scorecard_version_id=version.id).all()
    assert len(nodes) == 6
    assert sum(float(n.weight) for n in nodes) == 100

    evaluations = db_session.query(Evaluation).filter_by(scorecard_version_id=version.id).all()
    assert len(evaluations) == 2
    assert {e.evaluated_by for e in evaluations} == {evaluator_a.id, evaluator_b.id}
    for e in evaluations:
        assert e.final_weighted_score is not None
        assert e.rag_band is not None

    statuses = {r.status for r in report.results}
    assert statuses == {"created"}


# --- boundary cases ---


def test_scenario_boundary_cases(db_session: Session) -> None:
    owner = _owner(db_session)
    report = ScenarioReport()
    scenario_boundary_cases(db_session, report, owner)
    db_session.commit()

    zero_weight_node = (
        db_session.query(KpiNode).filter_by(name="Not-yet-weighted criterion (0%)").one()
    )
    assert float(zero_weight_node.weight) == 0

    single_scorecard = (
        db_session.query(Scorecard).filter_by(name="Boundary: Single-KPI Scorecard").one()
    )
    single_nodes = (
        db_session.query(KpiNode)
        .filter_by(scorecard_version_id=single_scorecard.current_version_id)
        .all()
    )
    assert len(single_nodes) == 1
    assert float(single_nodes[0].weight) == 100

    assert all(r.status == "created" for r in report.results)


# --- invalid / flawed data ---


def test_scenario_invalid_weight_sum_is_rejected_and_reported(db_session: Session) -> None:
    owner = _owner(db_session)
    report = ScenarioReport()
    scenario_invalid_weight_sum(db_session, report, owner, target_total=97)
    db_session.commit()

    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "rejected_as_expected"
    assert "weight sum" in result.detail


def test_scenario_invalid_duplicate_names_is_rejected_and_reported(db_session: Session) -> None:
    owner = _owner(db_session)
    report = ScenarioReport()
    scenario_invalid_duplicate_names(db_session, report, owner)
    db_session.commit()

    assert len(report.results) == 1
    assert report.results[0].status == "rejected_as_expected"
    assert "uq_kpi_nodes_sibling_name" in report.results[0].detail


# --- migration cases ---


def test_scenario_version_bump_preserves_evaluations(db_session: Session) -> None:
    owner = _owner(db_session)
    report = ScenarioReport()
    scenario_version_bump_preserves_evaluations(db_session, report, owner)
    db_session.commit()

    assert len(report.results) == 1
    assert report.results[0].status == "created"


def test_scenario_embedding_model_backfill_updates_in_place(db_session: Session) -> None:
    owner = _owner(db_session)
    report = ScenarioReport()
    scenario_embedding_model_backfill(db_session, report, owner)
    db_session.commit()

    from app.models.scorecard_embedding import ScorecardEmbedding

    embeddings = db_session.query(ScorecardEmbedding).all()
    # Exactly one row — the backfill updates in place rather than inserting a second row.
    assert len(embeddings) == 1
    assert embeddings[0].embedding_model == "amazon.titan-embed-text-v2:0"
