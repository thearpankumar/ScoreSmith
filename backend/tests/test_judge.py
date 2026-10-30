"""Tests for the LLM judge's scoring math (app/ai/judge.py).

`test_effective_leaf_weights_and_rag_bands` is pure math — no DB, no Bedrock — proving the
weighted-score/RAG-band aggregation matches the framework's 7-band scale exactly, given
fixed fake per-KPI scores, per the task's explicit ask."""

from __future__ import annotations

import uuid

import pytest

from app.ai.judge import compute_weighted_score, effective_leaf_weights, leaf_nodes
from app.models.enums import RagBand, rag_band_for_score
from app.models.kpi_node import KpiNode


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
    """`id_a` is a category/grouping node (has children) and carries NO weight of its own
    (`weight=None` — see migration 0008_category_nodes_no_weight): a leaf's effective
    weight is simply its own `weight / 100` directly, never multiplied through an
    ancestor's weight, and every LEAF (`id_b`, `id_a1`, `id_a2`) sums to 100 together
    across the whole tree regardless of which (if any) category it sits under."""
    id_a, id_b, id_a1, id_a2 = (uuid.uuid4() for _ in range(4))
    nodes = [
        _plain_node(node_id=id_a, parent_id=None, weight=None, level=1),  # category: no weight
        _plain_node(node_id=id_b, parent_id=None, weight=30, level=1),  # leaf
        _plain_node(node_id=id_a1, parent_id=id_a, weight=35, level=2),  # leaf
        _plain_node(node_id=id_a2, parent_id=id_a, weight=35, level=2),  # leaf
    ]

    leaves = leaf_nodes(nodes)
    assert {n.id for n in leaves} == {id_b, id_a1, id_a2}

    weights = effective_leaf_weights(nodes)
    assert weights[id_b] == pytest.approx(0.30)
    assert weights[id_a1] == pytest.approx(0.35)  # flat, no multiplication by id_a (no weight)
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
