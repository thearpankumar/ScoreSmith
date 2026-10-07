"""Pure analytics for the export: ranking, distribution, category / KPI statistics, risk flags and the
plain-English "key takeaways". No I/O, no Excel — everything here is unit-testable.

Every judgement is TARGET-RELATIVE: a score is compared with the target of its own scorecard (the default
target when the scorecard has none), so "meets target" means the same thing for a target of 4 and of 9.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from app.reporting.export_data import UNCATEGORISED, ExportBundle, ExportEvaluation
from app.reporting.palette import (
    DEFAULT_TARGET,
    TARGET_BANDS,
    TargetBand,
    effective_target,
    target_band,
    target_is_default,
)
from app.reporting.xlsx_safety import clean_text

# "At risk" / "weak" are target-relative: the Well-below-target and Critical bands, i.e. under 75% of the target.
AT_RISK_KEYS = frozenset({"well_below", "critical"})
AT_RISK_TEXT = "below 75% of their target"
WEAK_KPI_SHARE = 0.30
WEAK_KPI_MIN_EVALS = 3
CONTESTED_STDEV = 2.0
MAX_TAKEAWAYS = 8


@dataclass
class ScoredRow:
    ev: ExportEvaluation
    score: float
    band: TargetBand  # relative to this evaluation's scorecard target
    rank: int
    tied: bool
    rank_in_card: int
    percentile: float | None
    delta_vs_mean: float
    target: float  # effective target (the default when the scorecard has none)
    target_defaulted: bool
    delta_vs_target: float

    @property
    def meets_target(self) -> bool:
        return self.band.key in ("exceeds", "meets")

    @property
    def at_risk(self) -> bool:
        return self.band.key in AT_RISK_KEYS


@dataclass
class CategoryStat:
    name: str
    mean: float
    low: float
    high: float
    n: int


@dataclass
class KpiStat:
    scorecard: str
    category: str
    name: str
    mean: float
    n: int
    weak_share: float
    stdev: float
    needs_review: int
    target: float = DEFAULT_TARGET


@dataclass
class Flag:
    severity: str  # "high" | "medium" | "info"
    count: int
    label: str
    details: str


@dataclass
class Insights:
    scored: list[ScoredRow]
    excluded: list[ExportEvaluation]  # in the bundle but not exportable (failed / not completed / no score)
    n_total: int  # exported evaluations (== n_scored: only scored evaluations are ever exported)
    n_scored: int
    not_exported: int  # selected but not exported: deleted + failed / not completed / unscored
    mean: float | None
    median: float | None
    stdev: float | None
    highest: ScoredRow | None
    lowest: ScoredRow | None
    targets: list[float]  # distinct effective targets of the exported scorecards, ascending
    common_target: float | None  # the single target when every exported scorecard shares it
    display_target: float  # target used for aggregated cells (averages): the common one, else the default
    target_defaulted: bool  # some scorecard has no target, so DEFAULT_TARGET was assumed for it
    pass_count: int  # evaluations meeting or exceeding THEIR OWN target
    pass_pct: float | None
    at_risk: list[ScoredRow]
    distribution: list[tuple[TargetBand, int, float]]
    categories: list[CategoryStat]
    kpi_stats: list[KpiStat]
    strongest_kpis: list[KpiStat]
    weakest_kpis: list[KpiStat]
    flags: list[Flag]
    takeaways: list[str]
    multi_scorecard: bool
    scorecard_names: list[str]
    needs_review_total: int
    missing_email: list[ExportEvaluation]
    top: list[ScoredRow] = field(default_factory=list)
    attention: list[ScoredRow] = field(default_factory=list)


def _nm(ev: ExportEvaluation) -> str:
    return clean_text(ev.display_name, 80)


def _fmt(x: float) -> str:
    return f"{x:.1f}"


def rank_scored(evals: list[ExportEvaluation]) -> list[ScoredRow]:
    """Competition ranking (1, 2, 2, 4) on the 2-dp overall score, descending; ties ordered by name, email, id."""
    scored = [e for e in evals if e.is_scored]
    scored.sort(key=lambda e: (-round(e.score or 0, 2), e.display_name.lower(), (e.subject_email or "").lower(), str(e.id)))
    n = len(scored)
    mean = statistics.fmean(e.score for e in scored) if scored else 0.0  # type: ignore[misc]
    rows: list[ScoredRow] = []
    counts: dict[float, int] = defaultdict(int)
    for e in scored:
        counts[round(e.score, 2)] += 1  # type: ignore[arg-type]
    card_seen: dict[uuid.UUID, list[float]] = defaultdict(list)
    prev_score: float | None = None
    prev_rank = 0
    for i, e in enumerate(scored, start=1):
        s = round(e.score, 2)  # type: ignore[arg-type]
        rank = prev_rank if s == prev_score else i
        prev_score, prev_rank = s, rank
        in_card = card_seen[e.scorecard_id]
        card_rank = (in_card.index(s) + 1) if s in in_card else len(in_card) + 1
        in_card.append(s)
        target = effective_target(e.target)
        rows.append(
            ScoredRow(
                ev=e, score=s, band=target_band(s, e.target), rank=rank, tied=counts[s] > 1, rank_in_card=card_rank,
                percentile=((n - rank) / (n - 1)) if n > 1 else None, delta_vs_mean=round(s - mean, 2),
                target=target, target_defaulted=target_is_default(e.target), delta_vs_target=round(s - target, 2),
            )
        )
    return rows


def _pick(stats: list[KpiStat], k: int) -> tuple[list[KpiStat], list[KpiStat]]:
    if not stats:
        return [], []
    ordered = sorted(stats, key=lambda s: (-s.mean, s.name))
    if len(ordered) == 1:
        return ordered, []
    k = min(k, len(ordered) // 2)
    return ordered[:k], sorted(ordered[-k:], key=lambda s: (s.mean, s.name))


def compute_insights(bundle: ExportBundle) -> Insights:
    all_evals = bundle.evaluations
    evals = [e for e in all_evals if e.is_scored]  # only scored evaluations are ever exported
    excluded = [e for e in all_evals if not e.is_scored]
    rows = rank_scored(evals)
    scores = [r.score for r in rows]
    n = len(rows)
    mean = statistics.fmean(scores) if n else None
    median = statistics.median(scores) if n else None
    stdev = statistics.pstdev(scores) if n >= 3 else None

    cards = {e.scorecard_id: e.scorecard_name for e in evals}
    scored_cards = {r.ev.scorecard_id for r in rows}
    multi = len(scored_cards) > 1
    targets = sorted({r.target for r in rows})
    common_target = targets[0] if len(targets) == 1 else None
    display_target = common_target if common_target is not None else DEFAULT_TARGET
    target_defaulted = any(r.target_defaulted for r in rows)
    pass_count = sum(1 for r in rows if r.meets_target)

    at_risk = [r for r in rows if r.at_risk]
    dist = [(b, sum(1 for r in rows if r.band.key == b.key)) for b in TARGET_BANDS]
    dist = [(b, c, (c / n) if n else 0.0) for b, c in dist]

    # --- categories (aggregated by name across all scored evaluations) ---
    cat_vals: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        for cat, val in r.ev.category_scores.items():
            if val is not None and cat != UNCATEGORISED:
                cat_vals[cat].append(val)
    categories = [
        CategoryStat(c, statistics.fmean(v), min(v), max(v), len(v)) for c, v in cat_vals.items()
    ]
    categories.sort(key=lambda c: (-c.mean, c.name))

    # --- KPI stats (per scorecard, keyed by KPI path so versions of one scorecard merge) ---
    bucket: dict[tuple[uuid.UUID, str], dict] = {}
    for r in rows:
        kpis = {k.id: k for k in bundle.kpis_by_version.get(r.ev.version_id, [])}
        for kid, res in r.ev.results.items():
            k = kpis.get(kid)
            if k is None or not k.is_leaf:
                continue
            b = bucket.setdefault(
                (r.ev.scorecard_id, k.path_text),
                {"card": r.ev.scorecard_name, "cat": k.category, "name": k.name, "v": [], "review": 0,
                 "target": r.target, "weak": 0},
            )
            b["v"].append(res.score)
            b["weak"] += 1 if target_band(res.score, r.ev.target).key in AT_RISK_KEYS else 0
            b["review"] += 1 if res.needs_review else 0
    kpi_stats: list[KpiStat] = []
    for b in bucket.values():
        v = b["v"]
        kpi_stats.append(
            KpiStat(
                scorecard=b["card"], category=b["cat"], name=b["name"], mean=statistics.fmean(v), n=len(v),
                weak_share=b["weak"] / len(v), stdev=statistics.pstdev(v) if len(v) >= 3 else 0.0,
                needs_review=b["review"], target=b["target"],
            )
        )
    if multi:
        strongest, weakest = [], []
        by_card: dict[str, list[KpiStat]] = defaultdict(list)
        for s in kpi_stats:
            by_card[s.scorecard].append(s)
        for card in sorted(by_card)[:3]:
            top, bottom = _pick(by_card[card], 3)
            strongest += top
            weakest += bottom
    else:
        strongest, weakest = _pick(kpi_stats, 5)

    needs_review_total = sum(1 for r in rows for res in r.ev.results.values() if res.needs_review)
    missing_email = [r.ev for r in rows if not (r.ev.subject_email or "").strip()]
    top: list[ScoredRow] = []
    attention: list[ScoredRow] = []
    if n >= 2:
        k = min(5, n // 2)  # never overlaps: top k and bottom k are disjoint
        top = rows[:k]
        attention = list(reversed(rows[-k:]))

    ins = Insights(
        scored=rows, excluded=excluded, n_total=len(evals), n_scored=n,
        not_exported=len(bundle.missing_ids) + bundle.excluded_count + len(excluded), mean=mean, median=median,
        stdev=stdev, highest=rows[0] if rows else None, lowest=rows[-1] if rows else None, targets=targets,
        common_target=common_target, display_target=display_target, target_defaulted=target_defaulted,
        pass_count=pass_count, pass_pct=(pass_count / n) if n else None, at_risk=at_risk, distribution=dist,
        categories=categories, kpi_stats=kpi_stats, strongest_kpis=strongest, weakest_kpis=weakest, flags=[],
        takeaways=[], multi_scorecard=multi,
        scorecard_names=sorted({cards[c] for c in scored_cards} or set(cards.values())),
        needs_review_total=needs_review_total, missing_email=missing_email, top=top, attention=attention,
    )
    ins.flags = _flags(ins)
    ins.takeaways = _takeaways(ins)
    return ins


def _names(rows: list[ExportEvaluation] | list[ScoredRow], limit: int = 5) -> str:
    items = [_nm(r.ev if isinstance(r, ScoredRow) else r) for r in rows]
    extra = len(items) - limit
    return ", ".join(items[:limit]) + (f" and {extra} more" if extra > 0 else "")


def target_phrase(ins: Insights) -> str:
    """'a target of 7.0', 'a target of 7.0 (no target set; the default is assumed)' or 'different targets'."""
    if ins.common_target is not None:
        note = " (no target is set on the scorecard; the default is assumed)" if ins.target_defaulted else ""
        return f"a target of {ins.common_target:.1f}{note}"
    return "targets that differ by scorecard (" + ", ".join(f"{t:.1f}" for t in ins.targets) + ")"


def _flags(ins: Insights) -> list[Flag]:
    flags: list[Flag] = []
    if ins.at_risk:
        flags.append(Flag("high", len(ins.at_risk), f"Evaluations {AT_RISK_TEXT}",
                          f"{_names(ins.at_risk)}. See the “Needs attention” list and the Leaderboard."))
    weak = [s for s in ins.kpi_stats if s.n >= WEAK_KPI_MIN_EVALS and s.weak_share >= WEAK_KPI_SHARE]
    if weak:
        weak.sort(key=lambda s: (-s.weak_share, s.name))
        flags.append(Flag("high", len(weak), f"KPIs well below target in ≥{WEAK_KPI_SHARE:.0%} of evaluations",
                          "; ".join(f"{clean_text(s.name, 60)} ({s.weak_share:.0%})" for s in weak[:5]) + ". See KPI Matrix."))
    contested = [s for s in ins.kpi_stats if s.n >= WEAK_KPI_MIN_EVALS and s.stdev >= CONTESTED_STDEV]
    if contested:
        contested.sort(key=lambda s: -s.stdev)
        flags.append(Flag("medium", len(contested), f"KPIs with very uneven scores (spread ≥ {CONTESTED_STDEV:.1f})",
                          "; ".join(f"{clean_text(s.name, 60)} (σ {s.stdev:.1f})" for s in contested[:5]) + ". See KPI Matrix."))
    if ins.needs_review_total:
        flags.append(Flag("medium", ins.needs_review_total, "KPI scores flagged for human review",
                          "The AI judge's runs disagreed or confidence was low. Filter “Needs review” on the KPI Detail sheet."))
    if ins.missing_email:
        flags.append(Flag("info", len(ins.missing_email), "Evaluations with no subject email", _names(ins.missing_email)))
    if ins.multi_scorecard:
        flags.append(Flag("info", len(ins.scorecard_names), "Evaluations come from different scorecards",
                          "Scores may not be directly comparable; ranks are also shown within each scorecard."))
    if len(ins.targets) > 1:
        flags.append(Flag("info", len(ins.targets), "Scorecards have different targets",
                          "Colours and “vs target” compare each score with its own scorecard's target "
                          f"({', '.join(f'{t:.1f}' for t in ins.targets)})."))
    if ins.target_defaulted:
        flags.append(Flag("info", sum(1 for r in ins.scored if r.target_defaulted), "No target score set",
                          f"At least one scorecard has no target, so {DEFAULT_TARGET:.1f} is assumed for it. "
                          "Set a target on the scorecard's Overview page to colour its scores against it."))
    return flags


def _takeaways(ins: Insights) -> list[str]:
    out: list[str] = []
    n = ins.n_scored
    if n == 0:
        return ["None of the selected evaluations has a final score yet, so there is nothing to rank or summarise."]
    hi, lo = ins.highest, ins.lowest
    assert hi is not None and lo is not None and ins.mean is not None and ins.median is not None
    if n == 1:
        sign = f"{hi.delta_vs_target:+.1f}"
        out.append(f"{_nm(hi.ev)} scored {hi.score:.1f} / 10: {hi.band.label} ({sign} vs a target of {hi.target:.1f}).")
        if ins.categories:
            strong, weak = ins.categories[0], ins.categories[-1]
            if len(ins.categories) >= 2:
                out.append(f"Strongest category: {clean_text(strong.name, 60)} ({_fmt(strong.mean)}). "
                           f"Weakest category: {clean_text(weak.name, 60)} ({_fmt(weak.mean)}).")
        if ins.strongest_kpis and ins.weakest_kpis:
            out.append(f"Strongest KPI: {clean_text(ins.strongest_kpis[0].name, 60)} ({_fmt(ins.strongest_kpis[0].mean)}). "
                       f"Weakest KPI: {clean_text(ins.weakest_kpis[0].name, 60)} ({_fmt(ins.weakest_kpis[0].mean)}).")
        if ins.at_risk:
            out.append("This score is below 75% of the target, which needs attention.")
    else:
        out.append(f"{ins.n_total} evaluations are included. The average score is {ins.mean:.1f} / 10 "
                   f"(median {ins.median:.1f}) against {target_phrase(ins)}; "
                   f"{ins.pass_pct:.0%} meet or exceed their target.")  # type: ignore[misc]
        out.append(f"Highest: {_nm(hi.ev)} at {hi.score:.1f} ({hi.band.label}). Lowest: {_nm(lo.ev)} at {lo.score:.1f} "
                   f"({lo.band.label}), a gap of {hi.score - lo.score:.1f} points.")
        if len(ins.categories) >= 2:
            strong, weak = ins.categories[0], ins.categories[-1]
            out.append(f"Strongest area: {clean_text(strong.name, 60)} (average {_fmt(strong.mean)}). "
                       f"Weakest area: {clean_text(weak.name, 60)} (average {_fmt(weak.mean)}).")
        if ins.strongest_kpis and ins.weakest_kpis:
            out.append(f"Strongest KPI: {clean_text(ins.strongest_kpis[0].name, 60)} ({_fmt(ins.strongest_kpis[0].mean)}). "
                       f"Weakest KPI: {clean_text(ins.weakest_kpis[0].name, 60)} ({_fmt(ins.weakest_kpis[0].mean)}).")
        if ins.at_risk:
            out.append(f"{len(ins.at_risk)} evaluation{'s' if len(ins.at_risk) != 1 else ''} "
                       f"({len(ins.at_risk) / n:.0%}) {'are' if len(ins.at_risk) != 1 else 'is'} {AT_RISK_TEXT} "
                       "(Well below target or Critical) and need attention.")
    weak_kpis = sorted((s for s in ins.kpi_stats if s.n >= WEAK_KPI_MIN_EVALS and s.weak_share >= WEAK_KPI_SHARE),
                       key=lambda s: -s.weak_share)
    if weak_kpis:
        w = weak_kpis[0]
        out.append(f"{clean_text(w.name, 60)} is well below target in {w.weak_share:.0%} of evaluations.")
    if ins.needs_review_total:
        out.append(f"{ins.needs_review_total} KPI score{'s are' if ins.needs_review_total != 1 else ' is'} flagged for human review.")
    if ins.missing_email:
        out.append(f"{len(ins.missing_email)} evaluation{'s have' if len(ins.missing_email) != 1 else ' has'} no subject email.")
    if ins.multi_scorecard:
        out.append("Evaluations from different scorecards are mixed; compare scores across scorecards with care.")
    return [t for t in out if t][:MAX_TAKEAWAYS]
