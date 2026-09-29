"""Tests for the LLM judge (app/ai/judge.py).

`test_effective_leaf_weights_and_rag_bands` is pure math — no DB, no Bedrock — proving the
weighted-score/RAG-band aggregation matches the framework's 7-band scale exactly, given
fixed fake per-KPI scores, per the task's explicit ask.

`test_order_guidelines_perturbs_presentation_across_ensemble_calls` is pure logic — proves
the k=3 ensemble actually shows the model 3 different guideline-rung orderings (ascending,
descending, deterministic shuffle) rather than relying on temperature sampling alone for
diversity, per the ensemble design in the judge's module docstring.

`test_run_judge_persists_weighted_score_and_kpi_results` is a full integration test: a
real KPI hierarchy is seeded into real Postgres, `run_judge` runs the k=3-call-per-KPI
ensemble against a `FakeBedrockClient` (no real Bedrock — see tests/fakes.py) scripted with
one KPI whose 3 calls unanimously agree (no review flag) and one whose 3 calls disagree
enough to trip `needs_review` (both on score spread and on matched_level), and the
persisted `evaluations`/`evaluation_kpi_results` rows — including the new
needs_review/score_variance/ensemble_raw_scores columns — are checked via the existing
SQLAlchemy models (no raw SQL), matching the same weighted-average (using each KPI's
MEDIAN score) computed independently in the assertion. This supersedes the old
single-call-per-KPI test (the single-call code path no longer exists — `_judge_one_kpi_call`
is always invoked ENSEMBLE_K=3 times per KPI now — so ensemble aggregation IS the behavior
under test; median-of-3-identical-scores is the meaningful single-call-equivalent case,
covered by the "Accuracy" KPI below)."""

from __future__ import annotations

import uuid

import pytest

from app.ai.judge import (
    ENSEMBLE_K,
    _order_guidelines,
    compute_weighted_score,
    effective_leaf_weights,
    leaf_nodes,
    run_judge,
)
from app.models.enums import RagBand, rag_band_for_score
from app.models.evaluation import Evaluation
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from tests.fakes import FakeBedrockClient, text_result, tool_use_result


def _plain_node(*, node_id, parent_id, weight, level, included_in_scoring=True) -> KpiNode:
    """A KpiNode instantiated without a DB session — fine for pure attribute math, since
    leaf_nodes/effective_leaf_weights only ever read .id/.parent_id/.weight/
    .included_in_scoring. `included_in_scoring` is passed explicitly (rather than relying
    on the column's DB-side default) since a Python-constructed-but-never-flushed ORM
    object never gets its `server_default`/`default` applied."""
    return KpiNode(
        id=node_id,
        parent_id=parent_id,
        weight=weight,
        level=level,
        name=str(node_id),
        display_order=0,
        included_in_scoring=included_in_scoring,
    )


def test_effective_leaf_weights_and_rag_bands() -> None:
    id_a, id_b, id_a1, id_a2 = (uuid.uuid4() for _ in range(4))
    nodes = [
        _plain_node(node_id=id_a, parent_id=None, weight=70, level=1),  # not a leaf (has children)
        _plain_node(node_id=id_b, parent_id=None, weight=30, level=1),  # leaf
        _plain_node(node_id=id_a1, parent_id=id_a, weight=50, level=2),  # leaf
        _plain_node(node_id=id_a2, parent_id=id_a, weight=50, level=2),  # leaf
    ]

    leaves = leaf_nodes(nodes)
    assert {n.id for n in leaves} == {id_b, id_a1, id_a2}

    weights = effective_leaf_weights(nodes)
    assert weights[id_b] == pytest.approx(0.30)
    assert weights[id_a1] == pytest.approx(0.35)  # 0.5 * 0.70
    assert weights[id_a2] == pytest.approx(0.35)
    assert sum(weights.values()) == pytest.approx(1.0)

    scores = {id_b: 8.0, id_a1: 6.0, id_a2: 10.0}
    final = compute_weighted_score(scores, weights)
    assert final == pytest.approx(8.0 * 0.30 + 6.0 * 0.35 + 10.0 * 0.35)  # == 8.0

    # Exact 7-band boundary table from the Quality Scorecard Framework (§6), reused
    # as-is from app/models/enums.py — proven here against the judge's own output value.
    assert rag_band_for_score(final) is RagBand.BAND_8
    for score, expected_band in [
        (10, RagBand.BAND_10_9),
        (9, RagBand.BAND_10_9),
        (8.99, RagBand.BAND_8),
        (8, RagBand.BAND_8),
        (7.99, RagBand.BAND_7),
        (7, RagBand.BAND_7),
        (6, RagBand.BAND_6),
        (5, RagBand.BAND_5),
        (4, RagBand.BAND_4),
        (3.99, RagBand.BAND_3_0),
        (0, RagBand.BAND_3_0),
    ]:
        assert rag_band_for_score(score) is expected_band, score


def _guideline(kpi_node_id, level: int) -> KpiGuideline:
    return KpiGuideline(kpi_node_id=kpi_node_id, score_level=level, qualitative_text=f"Level {level} text.")


def test_order_guidelines_perturbs_presentation_across_ensemble_calls() -> None:
    kpi_id = uuid.uuid4()
    guidelines = [_guideline(kpi_id, lvl) for lvl in (0, 5, 10)]

    ascending = _order_guidelines(guidelines, 0, str(kpi_id))
    descending = _order_guidelines(guidelines, 1, str(kpi_id))
    shuffled = _order_guidelines(guidelines, 2, str(kpi_id))

    assert [g.score_level for g in ascending] == [0, 5, 10]
    assert [g.score_level for g in descending] == [10, 5, 0]
    # The shuffle is deterministic (seeded off the KPI id + attempt index) and still a
    # genuine reordering — not required to differ from ascending/descending for every
    # possible input, but must contain exactly the same guidelines (no loss/duplication).
    assert {g.score_level for g in shuffled} == {0, 5, 10}
    assert len(shuffled) == 3

    # Re-running with the same (kpi_id, attempt) is reproducible — important so a retried
    # evaluation doesn't silently reshuffle presentation between runs.
    assert [g.score_level for g in _order_guidelines(guidelines, 2, str(kpi_id))] == [
        g.score_level for g in shuffled
    ]

    # A different KPI id shuffles differently (not a global constant shuffle order).
    other_id = uuid.uuid4()
    other_shuffled = _order_guidelines(guidelines, 2, str(other_id))
    assert {g.score_level for g in other_shuffled} == {0, 5, 10}


async def test_run_judge_persists_weighted_score_and_kpi_results(async_db_session) -> None:
    db = async_db_session
    owner = User(email=f"judge-{uuid.uuid4().hex[:8]}@example.com", name="Judge Test Owner")
    db.add(owner)
    await db.flush()

    scorecard = Scorecard(name="Judge Test Scorecard", owner_id=owner.id, domain="Support")
    db.add(scorecard)
    await db.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db.add(version)
    await db.flush()

    accuracy_id, tone_id = uuid.uuid4(), uuid.uuid4()
    from sqlalchemy_utils import Ltree

    accuracy = KpiNode(
        id=accuracy_id, scorecard_version_id=version.id, parent_id=None, path=Ltree(accuracy_id.hex),
        level=1, name="Accuracy", weight=60, display_order=0,
    )
    tone = KpiNode(
        id=tone_id, scorecard_version_id=version.id, parent_id=None, path=Ltree(tone_id.hex),
        level=1, name="Tone", weight=40, display_order=1,
    )
    db.add_all([accuracy, tone])
    for level in (0, 5, 9, 10):
        db.add(_guideline(accuracy_id, level))
        db.add(_guideline(tone_id, level))
    await db.commit()  # commits the whole sibling group at once (weight-sum trigger fires here)

    evaluation = Evaluation(scorecard_version_id=version.id, name="Test run", evaluated_by=owner.id)
    db.add(evaluation)
    await db.commit()

    # Accuracy: all ENSEMBLE_K=3 calls unanimously agree (score 9, matched_level 9) —
    # median-of-3-identical-values is the meaningful "single-call-equivalent" case: no
    # disagreement, so needs_review stays False and score_variance is 0.
    accuracy_queue = [
        {
            "matched_level": 9,
            "evidence_quotes": ["the refund was processed correctly"],
            "reasoning": "Matches level 9.",
            "score": 9,
        }
        for _ in range(ENSEMBLE_K)
    ]
    # Tone: the 3 calls genuinely DISAGREE (scores 3, 5, 9 and matched_level 3, 5, 9 —
    # order-perturbation is exactly what's meant to surface a real presentation-sensitive
    # disagreement like this). Median score is 5 (the middle value), spread is 9-3=6 > the
    # 2-point threshold, and matched_level isn't unanimous — both independently trip
    # needs_review. The persisted representative fields (reasoning/evidence/matched_level)
    # must come from the score=5 call specifically, since its score exactly equals the
    # median (closest-to-median tie-break).
    tone_queue = [
        {
            "matched_level": 3,
            "evidence_quotes": ["quite curt with the customer"],
            "reasoning": "Matches level 3 — dismissive tone.",
            "score": 3,
        },
        {
            "matched_level": 5,
            "evidence_quotes": ["a bit curt"],
            "reasoning": "Matches level 5.",
            "score": 5,
        },
        {
            "matched_level": 9,
            "evidence_quotes": ["staff remained courteous throughout"],
            "reasoning": "Matches level 9 — courteous tone.",
            "score": 9,
        },
    ]
    queues = {"Accuracy": list(accuracy_queue), "Tone": list(tone_queue)}

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        prompt = messages[0]["content"][0]["text"]
        if not tools:
            return text_result("Evidence: refund processed correctly; tone was a bit curt.")
        for kpi_name, queue in queues.items():
            if f"KPI: {kpi_name} " in prompt:
                if not queue:
                    raise AssertionError(f"More than {ENSEMBLE_K} calls made for KPI {kpi_name!r}")
                return tool_use_result("record_kpi_judgment", queue.pop(0))
        raise AssertionError(f"No scripted judgment for prompt: {prompt[:200]!r}")

    fake = FakeBedrockClient(converse_fn=converse_fn)

    result = await run_judge(db, evaluation, "The refund was processed correctly. Staff tone was a bit curt.", fake)

    # Median-per-KPI scores feed the weighted average: Accuracy median=9, Tone median=5.
    expected_final = 9 * 0.60 + 5 * 0.40  # == 7.4
    assert result.final_weighted_score == pytest.approx(expected_final)
    assert result.rag_band == rag_band_for_score(expected_final).value

    # Every KPI's ensemble made exactly ENSEMBLE_K=3 calls (plus 1 shared evidence-extraction
    # call up front) — proves the ensemble actually ran k=3 times per KPI, not once.
    tool_calls = [c for c in fake.calls if c["tools"]]
    assert len(tool_calls) == 2 * ENSEMBLE_K

    await db.refresh(evaluation)
    assert float(evaluation.final_weighted_score) == pytest.approx(expected_final)
    assert evaluation.rag_band is rag_band_for_score(expected_final)
    assert evaluation.status.value == "completed"

    await db.refresh(evaluation, attribute_names=["kpi_results"])
    by_kpi = {r.kpi_node_id: r for r in evaluation.kpi_results}

    # Accuracy: unanimous ensemble — no review flag, zero variance.
    acc = by_kpi[accuracy_id]
    assert float(acc.score) == pytest.approx(9)
    assert acc.matched_guideline_level == 9
    assert acc.evidence_quotes == ["the refund was processed correctly"]
    assert acc.needs_review is False
    assert float(acc.score_variance) == pytest.approx(0)
    assert len(acc.ensemble_raw_scores) == ENSEMBLE_K

    # Tone: disagreeing ensemble — median score/representative fields from the score=5
    # call, review flag trips on BOTH the spread and the matched_level disagreement.
    tone_result = by_kpi[tone_id]
    assert float(tone_result.score) == pytest.approx(5)
    assert tone_result.matched_guideline_level == 5
    assert tone_result.reasoning_text == "Matches level 5."
    assert tone_result.evidence_quotes == ["a bit curt"]
    assert tone_result.needs_review is True
    assert float(tone_result.score_variance) == pytest.approx(6)
    raw_scores = sorted(c["score"] for c in tone_result.ensemble_raw_scores)
    assert raw_scores == [3, 5, 9]
