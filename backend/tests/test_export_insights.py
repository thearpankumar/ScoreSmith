"""Pure tests for the export's analytics (app/reporting/insights.py, palette.py, export_data rollups)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.models.enums import RagBand, rag_band_for_score
from app.reporting import palette as P
from app.reporting.export_data import UNCATEGORISED, ExportBundle, ExportResult, build_kpis, rollup_categories
from app.reporting.insights import compute_insights, rank_scored
from tests.reporting_fixtures import make_bundle, make_eval, make_version

RAG_TS = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "rag.ts"


# ---- palette parity -----------------------------------------------------------------------------------------
@pytest.mark.skipif(not RAG_TS.exists(), reason="frontend/lib/rag.ts not in this checkout")
def test_bands_match_frontend_rag_ts() -> None:
    src = RAG_TS.read_text(encoding="utf-8")
    found = re.findall(r'key: "([^"]+)", label: "([^"]+)", color: "(#[0-9A-Fa-f]{6})", min: ([0-9.]+)', src)
    assert len(found) == len(P.BANDS) == 7
    for (key, label, color, mn), band in zip(found, P.BANDS, strict=True):
        assert (key, label, color.upper(), float(mn)) == (band.key, band.label, band.fill.upper(), band.min_score)


def test_band_for_score_matches_backend_rag_band_boundaries() -> None:
    expected = {
        10: "excellent", 9.0: "excellent", 8.99: "good", 8.0: "good", 7.99: "acceptable", 7.0: "acceptable",
        6.99: "needs-improvement", 6.0: "needs-improvement", 5.99: "weak", 5.0: "weak", 4.99: "poor", 4.0: "poor",
        3.99: "critical", 0: "critical",
    }
    for score, key in expected.items():
        assert P.band_for_score(score).key == key, score
    # same thresholds as the backend enum helper
    assert rag_band_for_score(7.0) == RagBand.BAND_7 and P.band_for_score(7.0).fill == "#9CCC65"


def test_score_fill_interpolates_the_fixed_scale() -> None:
    assert P.score_fill(0) == P.SCALE_MIN_COLOR.upper()
    assert P.score_fill(5) == P.SCALE_MID_COLOR.upper()
    assert P.score_fill(10) == P.SCALE_MAX_COLOR.upper()
    assert P.score_fill(-3) == P.score_fill(0) and P.score_fill(99) == P.score_fill(10)


# ---- ranking ------------------------------------------------------------------------------------------------------
def test_competition_ranking_with_alphabetical_ties() -> None:
    bundle = make_bundle()
    rows = rank_scored(bundle.evaluations)
    assert [r.ev.subject_name for r in rows] == ["Priya Shah", "Omar Ali", "Asha Menon", "Dev Patel", "Mia Chen", "Rahul Kumar"]
    assert [r.rank for r in rows] == [1, 2, 3, 3, 5, 6]
    assert [r.tied for r in rows] == [False, False, True, True, False, False]
    assert rows[0].percentile == 1.0 and rows[-1].percentile == 0.0
    assert rows[2].percentile == pytest.approx((6 - 3) / 5)


def test_ranking_ignores_unscored_and_failed_evaluations() -> None:
    vid, kpis = make_version()
    ok = make_eval(vid, kpis, "A", "a@x.com", 8.0, [8, 8, 8, 8])
    failed = make_eval(vid, kpis, "B", "b@x.com", None, [], status="failed")
    queued = make_eval(vid, kpis, "C", "c@x.com", 9.9, [], status="queued")
    rows = rank_scored([ok, failed, queued])
    assert [r.ev.subject_name for r in rows] == ["A"]
    assert rows[0].percentile is None  # n == 1


def test_rank_within_scorecard() -> None:
    v1, k1 = make_version()
    v2, k2 = make_version()
    evals = [
        make_eval(v1, k1, "A", None, 9.0, [9] * 4, scorecard="Card One"),
        make_eval(v2, k2, "B", None, 8.0, [8] * 4, scorecard="Card Two"),
        make_eval(v2, k2, "C", None, 7.0, [7] * 4, scorecard="Card Two"),
    ]
    rows = rank_scored(evals)
    assert [(r.ev.subject_name, r.rank, r.rank_in_card) for r in rows] == [("A", 1, 1), ("B", 2, 1), ("C", 3, 2)]


# ---- roll-ups ------------------------------------------------------------------------------------------------------
def test_category_rollup_weighted_and_excludes_info_only_and_renormalises() -> None:
    vid, kpis = make_version()
    leaves = [k for k in kpis if k.is_leaf]
    comm1, comm2, tech1, tech2 = leaves
    results = {
        comm1.id: ExportResult(comm1.id, 10, 10, "", "", False, None, ""),
        comm2.id: ExportResult(comm2.id, 5, 5, "", "", False, None, ""),
        tech1.id: ExportResult(tech1.id, 6, 6, "", "", False, None, ""),
        # tech2 unscored -> renormalised over tech1 only
    }
    cats = rollup_categories(kpis, results)
    assert cats["Communication"] == pytest.approx((10 * 30 + 5 * 20) / 50)
    assert cats["Technical"] == pytest.approx(6.0)
    comm2.included = False  # info only -> excluded from the roll-up
    assert rollup_categories(kpis, results)["Communication"] == pytest.approx(10.0)
    assert rollup_categories(kpis, {})["Technical"] is None


def test_build_kpis_orders_depth_first_and_marks_leaves(monkeypatch) -> None:
    class N:
        def __init__(self, id, parent_id, name, order, weight=None):
            self.id, self.parent_id, self.name, self.display_order = id, parent_id, name, order
            self.level, self.weight, self.included_in_scoring = 1, weight, True

    a, b, a1, a2, b1 = (uuid.uuid4() for _ in range(5))
    nodes = [N(b1, b, "B1", 0, 50), N(a2, a, "A2", 1, 25), N(a, None, "A", 0), N(b, None, "B", 1), N(a1, a, "A1", 0, 25)]
    out = build_kpis(nodes, {})
    assert [k.name for k in out] == ["A", "A1", "A2", "B", "B1"]
    assert [k.is_leaf for k in out] == [False, True, True, False, True]
    assert out[1].category == "A" and out[1].path_text == "A › A1"
    loose = build_kpis([N(uuid.uuid4(), None, "Solo", 0, 100)], {})
    assert loose[0].category == UNCATEGORISED


# ---- insights --------------------------------------------------------------------------------------------------------
def test_distribution_boundaries_are_target_relative_and_percentages_sum_to_one() -> None:
    vid, kpis = make_version()
    # default target 7.0: exceeds >= 7.7, meets >= 7.0, near >= 6.3, below >= 5.25, well below >= 3.5
    scores = [10, 7.7, 7.69, 7.0, 6.99, 6.3, 6.29, 5.25, 5.24, 3.5, 3.49, 0.0]
    bundle = ExportBundle([make_eval(vid, kpis, f"P{i}", None, s, [s] * 4) for i, s in enumerate(scores)], {vid: kpis})
    ins = compute_insights(bundle)
    counts = {b.key: c for b, c, _ in ins.distribution}
    assert counts == {"exceeds": 2, "meets": 2, "near": 2, "below": 2, "well_below": 2, "critical": 2}
    assert sum(share for _, _, share in ins.distribution) == pytest.approx(1.0)


def test_bands_follow_each_evaluations_own_target() -> None:
    v1, k1 = make_version()
    v2, k2 = make_version()
    evals = [
        make_eval(v1, k1, "LowTarget", None, 4.2, [4] * 4, scorecard="Easy", target=4.0),
        make_eval(v2, k2, "HighTarget", None, 4.2, [4] * 4, scorecard="Hard", target=9.0),
    ]
    ins = compute_insights(ExportBundle(evals, {v1: k1, v2: k2}))
    by_name = {r.ev.subject_name: r for r in ins.scored}
    assert by_name["LowTarget"].band.key == "meets" and by_name["LowTarget"].delta_vs_target == pytest.approx(0.2)
    assert by_name["HighTarget"].band.key == "critical" and by_name["HighTarget"].delta_vs_target == pytest.approx(-4.8)
    assert ins.targets == [4.0, 9.0] and ins.common_target is None and ins.display_target == 7.0
    assert ins.pass_count == 1 and [r.ev.subject_name for r in ins.at_risk] == ["HighTarget"]
    assert any("Scorecards have different targets" in f.label for f in ins.flags)
    assert "differ by scorecard" in ins.takeaways[0]


def test_headline_stats_and_takeaways() -> None:
    ins = compute_insights(make_bundle())
    assert ins.n_total == ins.n_scored == 6
    assert ins.mean == pytest.approx((9.1 + 3.4 + 7.5 + 7.5 + 5.2 + 8.2) / 6)
    assert ins.targets == [7.0] and ins.common_target == 7.0 and ins.target_defaulted
    assert ins.pass_count == 4  # 9.1, 8.2, 7.5, 7.5 meet or exceed the (default) target 7.0
    assert [r.ev.subject_name for r in ins.at_risk] == ["Mia Chen", "Rahul Kumar"]  # 5.2 well below, 3.4 critical
    assert len(ins.takeaways) <= 8
    first = ins.takeaways[0]
    assert first.startswith("6 evaluations are included") and "target of 7.0" in first and "default is assumed" in first
    assert any("Highest: Priya Shah at 9.1" in t and "Lowest: Rahul Kumar at 3.4" in t for t in ins.takeaways)
    assert any("no subject email" in t for t in ins.takeaways)
    assert [r.ev.subject_name for r in ins.top][:2] == ["Priya Shah", "Omar Ali"]
    assert [r.ev.subject_name for r in ins.attention][0] == "Rahul Kumar"
    labels = [f.label for f in ins.flags]
    assert any("below 75% of their target" in lbl for lbl in labels) and any("no subject email" in lbl for lbl in labels)
    assert any(lbl == "No target score set" for lbl in labels)


def test_target_score_is_used_when_single_scorecard_shares_one() -> None:
    ins = compute_insights(make_bundle(target=8.0))
    assert ins.common_target == 8.0 and not ins.target_defaulted
    assert ins.pass_count == 2  # 9.1 exceeds and 8.2 meets a target of 8.0; the 7.5s are only "near"
    assert "target of 8.0" in ins.takeaways[0] and "default" not in ins.takeaways[0]
    assert {r.ev.subject_name: r.band.key for r in ins.scored}["Asha Menon"] == "near"
    assert not any(f.label == "No target score set" for f in ins.flags)


def test_single_evaluation_wording_and_no_ranking_noise() -> None:
    ins = compute_insights(make_bundle([("Solo", "solo@x.com", 8.3, [9, 8, 8, 8])]))
    assert ins.n_scored == 1 and ins.top == [] and ins.attention == []
    assert ins.takeaways[0].startswith("Solo scored 8.3 / 10: Exceeds target (+1.3 vs a target of 7.0)")
    assert not any("Highest" in t for t in ins.takeaways)


def test_no_scored_evaluations() -> None:
    vid, kpis = make_version()
    ins = compute_insights(ExportBundle([make_eval(vid, kpis, "X", None, None, [], status="failed")], {vid: kpis}))
    assert ins.n_scored == 0 and ins.mean is None and ins.n_total == 0
    assert len(ins.excluded) == 1 and ins.not_exported == 1  # counted, never listed
    assert "nothing to rank" in ins.takeaways[0]


def test_failed_and_unscored_evaluations_never_enter_statistics_or_flags() -> None:
    vid, kpis = make_version()
    ok = [make_eval(vid, kpis, f"Ok{i}", f"o{i}@x.com", 8.0 + i / 10, [8] * 4, target=7.0) for i in range(3)]
    bad = [make_eval(vid, kpis, "Failed One", None, None, [], status="failed", target=7.0),
           make_eval(vid, kpis, "Queued One", None, 3.0, [], status="queued", target=7.0)]
    bundle = ExportBundle(ok + bad, {vid: kpis}, missing_ids=[uuid.uuid4()], excluded_count=2)
    ins = compute_insights(bundle)
    assert ins.n_total == ins.n_scored == 3 and ins.not_exported == 1 + 2 + 2
    everyone = " ".join(ins.takeaways + [f.details for f in ins.flags])
    assert "Failed One" not in everyone and "Queued One" not in everyone


def test_takeaways_absent_when_precondition_empty() -> None:
    vid, kpis = make_version()
    bundle = ExportBundle(
        [make_eval(vid, kpis, "A", "a@x.com", 8.0, [8] * 4), make_eval(vid, kpis, "B", "b@x.com", 8.5, [8.5] * 4)],
        {vid: kpis},
    )
    ins = compute_insights(bundle)
    text = " ".join(ins.takeaways)
    assert "no subject email" not in text and "flagged for human review" not in text and "need attention" not in text
    assert [f.label for f in ins.flags] == ["No target score set"]  # nothing else is worth flagging


def test_recurring_weak_kpi_and_review_flags() -> None:
    vid, kpis = make_version()
    evals = [make_eval(vid, kpis, f"P{i}", f"p{i}@x.com", 6.0, [8, 8, 8, 2], needs_review=(i == 0)) for i in range(4)]
    ins = compute_insights(ExportBundle(evals, {vid: kpis}))
    assert any("Edge cases" in t and "well below target" in t and "100%" in t for t in ins.takeaways)
    assert any("flagged for human review" in t for t in ins.takeaways)
    assert ins.needs_review_total == 1
    assert ins.weakest_kpis[0].name == "Edge cases" and ins.weakest_kpis[0].weak_share == 1.0
    assert ins.strongest_kpis and ins.strongest_kpis[0].name != "Edge cases"


def test_mixed_scorecards_flagged() -> None:
    v1, k1 = make_version()
    v2, k2 = make_version()
    evals = [make_eval(v1, k1, "A", None, 9.0, [9] * 4, scorecard="One"), make_eval(v2, k2, "B", None, 5.0, [5] * 4, scorecard="Two")]
    ins = compute_insights(ExportBundle(evals, {v1: k1, v2: k2}))
    assert ins.multi_scorecard and ins.common_target == 7.0  # both scorecards fall back to the default target
    assert any("different scorecards" in f.label for f in ins.flags)
