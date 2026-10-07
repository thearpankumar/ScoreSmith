"""Loads the data an Excel export needs (a handful of queries, no N+1) into plain dataclasses, and rolls
leaf KPI scores up into top-level categories exactly like the UI's `computeKpiRollup`."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard_version import ScorecardVersion
from app.reporting.xlsx_safety import clean_text

UNCATEGORISED = "(Uncategorised)"


@dataclass
class ExportKpi:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    level: int
    weight: float | None
    included: bool
    is_leaf: bool
    order: int  # depth-first display position within the version
    path: tuple[str, ...] = ()  # names root -> self
    category: str = UNCATEGORISED  # level-1 ancestor name (leaves only)
    guidelines: dict[int, str] = field(default_factory=dict)

    @property
    def path_text(self) -> str:
        return " › ".join(self.path)

    @property
    def guideline_summary(self) -> str:
        parts = [f"{lvl}: {self.guidelines[lvl]}" for lvl in (0, 5, 10) if lvl in self.guidelines]
        return "\n".join(parts)


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of evidence behind a KPI score: a verbatim quote and, when known, where it came from."""

    quote: str
    source: str = ""


@dataclass
class ExportResult:
    kpi_id: uuid.UUID
    score: float
    matched_level: int | None
    reasoning: str
    evidence: list[EvidenceItem]
    needs_review: bool
    variance: float | None
    ensemble: str

    def __post_init__(self) -> None:  # tolerate raw text / JSON shapes (tests, callers) and always hold items
        if not isinstance(self.evidence, list) or any(not isinstance(i, EvidenceItem) for i in self.evidence):
            self.evidence = normalize_evidence(self.evidence)

    @property
    def evidence_text(self) -> str:
        """The evidence as one text block (one bullet per quote, source after a dash) for flat sheets."""
        return "\n".join(f"• “{i.quote}”" + (f" — {i.source}" if i.source else "") for i in self.evidence)

    @property
    def evidence_sources(self) -> str:
        return "; ".join(dict.fromkeys(i.source for i in self.evidence if i.source))


@dataclass
class ExportEvaluation:
    id: uuid.UUID
    name: str  # evaluation name
    subject_name: str | None
    subject_email: str | None
    scorecard_id: uuid.UUID
    scorecard_name: str
    version_id: uuid.UUID
    version_number: int
    domain: str | None
    status: str
    score: float | None  # stored, authoritative overall
    target: float | None
    evaluator_name: str
    source: str
    submitted_at: datetime | None
    finished_at: datetime | None
    error: str | None
    custom_formula: bool
    results: dict[uuid.UUID, ExportResult] = field(default_factory=dict)
    category_scores: dict[str, float | None] = field(default_factory=dict)
    kpis_total: int = 0

    @property
    def display_name(self) -> str:
        return (self.subject_name or "").strip() or self.name

    @property
    def is_scored(self) -> bool:
        return self.status == EvaluationStatus.COMPLETED.value and self.score is not None


@dataclass
class ExportBundle:
    evaluations: list[ExportEvaluation]
    kpis_by_version: dict[uuid.UUID, list[ExportKpi]]
    missing_ids: list[uuid.UUID] = field(default_factory=list)
    excluded_count: int = 0  # found but not exportable (failed / not completed / no final score); never exported


_QUOTE_KEYS = ("quote", "text", "snippet", "excerpt", "evidence", "content", "verbatim")
_SOURCE_KEYS = ("source", "label", "section", "section_id", "page", "ref", "reference", "location", "file", "document")
_BULLET = re.compile(r"^\s*(?:[•\-\*·▪●]\s+|\d+[.)]\s+)")
MAX_QUOTE_CHARS = 8000


def _one(value: object) -> str:
    return clean_text(str(value), MAX_QUOTE_CHARS).strip() if value is not None else ""


def _item_from_dict(d: dict) -> EvidenceItem | None:
    quote = next((_one(d[k]) for k in _QUOTE_KEYS if d.get(k) not in (None, "")), "")
    src_parts = [_one(d[k]) for k in _SOURCE_KEYS if d.get(k) not in (None, "")]
    if not quote:  # unknown shape: show it as "key: value" so nothing is silently lost
        quote = _one("; ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, "")))
        src_parts = []
    return EvidenceItem(quote, " · ".join(dict.fromkeys(p for p in src_parts if p))) if quote else None


def normalize_evidence(raw: object) -> list[EvidenceItem]:
    """Any stored evidence shape (list of strings, list of {quote, source...} objects, a dict, a plain or bulleted
    string, None) -> clean, de-duplicated `EvidenceItem`s in the original order."""
    items: list[EvidenceItem] = []
    if raw in (None, "", [], {}):
        return items
    if isinstance(raw, str):
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if len(lines) > 1 and all(_BULLET.match(ln) for ln in lines):
            items = [EvidenceItem(_one(_BULLET.sub("", ln, count=1))) for ln in lines]
        else:
            items = [EvidenceItem(_one(_BULLET.sub("", raw, count=1) if len(lines) <= 1 else raw))]
    elif isinstance(raw, dict):
        if any(k in raw for k in _QUOTE_KEYS):
            it = _item_from_dict(raw)
            items = [it] if it else []
        else:
            for key, val in raw.items():
                for v in val if isinstance(val, list) else [val]:
                    if isinstance(v, dict):
                        it = _item_from_dict(v)
                        if it:
                            items.append(EvidenceItem(it.quote, it.source or _one(key)))
                    elif _one(v):
                        items.append(EvidenceItem(_one(v), _one(key)))
    elif isinstance(raw, list | tuple):
        for entry in raw:
            if isinstance(entry, dict):
                it = _item_from_dict(entry)
                if it:
                    items.append(it)
            elif isinstance(entry, list | tuple):
                items.extend(normalize_evidence(list(entry)))
            elif _one(entry):
                items.append(EvidenceItem(_one(_BULLET.sub("", str(entry), count=1))))
    else:
        items = [EvidenceItem(_one(raw))]
    seen: set[tuple[str, str]] = set()
    out: list[EvidenceItem] = []
    for it in items:
        key = (it.quote, it.source)
        if it.quote and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _ensemble_text(raw: object) -> str:
    if isinstance(raw, list):
        return ", ".join(str(x) for x in raw)
    if isinstance(raw, dict):
        return ", ".join(f"{k}: {v}" for k, v in raw.items())
    return ""


def build_kpis(nodes: list[KpiNode], guidelines: dict[uuid.UUID, dict[int, str]]) -> list[ExportKpi]:
    """Depth-first ordered KPI list for one scorecard version, rebuilt from `parent_id`."""
    by_parent: dict[uuid.UUID | None, list[KpiNode]] = {}
    for n in nodes:
        by_parent.setdefault(n.parent_id, []).append(n)
    for kids in by_parent.values():
        kids.sort(key=lambda n: (n.display_order, n.name))
    out: list[ExportKpi] = []

    def walk(parent: uuid.UUID | None, trail: tuple[str, ...]) -> None:
        for n in by_parent.get(parent, []):
            path = (*trail, n.name)
            out.append(
                ExportKpi(
                    id=n.id, parent_id=n.parent_id, name=n.name, level=n.level,
                    weight=float(n.weight) if n.weight is not None else None,
                    included=bool(n.included_in_scoring), is_leaf=n.id not in by_parent, order=len(out),
                    path=path, category=trail[0] if trail else UNCATEGORISED,
                    guidelines=guidelines.get(n.id, {}),
                )
            )
            walk(n.id, path)

    walk(None, ())
    return out


def rollup_categories(kpis: list[ExportKpi], results: dict[uuid.UUID, ExportResult]) -> dict[str, float | None]:
    """Weighted average of the scored, scoring-included leaves of each top-level category, renormalised over the
    leaves that actually have a score (mirrors the frontend's `computeKpiRollup`). None = no scored leaves."""
    acc: dict[str, list[float]] = {}  # category -> [sum(score*w), sum(w), sum(score), n]
    for k in kpis:
        if not k.is_leaf:
            continue
        a = acc.setdefault(k.category, [0.0, 0.0, 0.0, 0.0])
        r = results.get(k.id)
        if r is None or not k.included:
            continue
        w = k.weight or 0.0
        a[0] += r.score * w
        a[1] += w
        a[2] += r.score
        a[3] += 1
    out: dict[str, float | None] = {}
    for cat, (s_w, w, s, n) in acc.items():
        out[cat] = (s_w / w if w > 0 else s / n) if n else None
    return out


def _is_exportable(e: Evaluation) -> bool:
    status = e.status.value if hasattr(e.status, "value") else str(e.status)
    return status == EvaluationStatus.COMPLETED.value and e.final_weighted_score is not None


async def load_export_bundle(db: AsyncSession, ids: list[uuid.UUID]) -> ExportBundle:
    """Query 1: evaluations (+version, scorecard, evaluator). 2: KPI results. 3: KPI nodes. 4: guidelines."""
    ordered = list(dict.fromkeys(ids))
    rows = (
        (
            await db.execute(
                select(Evaluation)
                .where(Evaluation.id.in_(ordered))
                .options(
                    joinedload(Evaluation.scorecard_version).joinedload(ScorecardVersion.scorecard),
                    joinedload(Evaluation.evaluator),
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    by_id = {e.id: e for e in rows}
    missing = [i for i in ordered if i not in by_id]
    found = [by_id[i] for i in ordered if i in by_id]
    # Only completed evaluations with a final score are ever exported: failed, queued, running or unscored ones are
    # dropped here (counted, never listed) so they cannot reach any sheet, ranking or statistic.
    evals = [e for e in found if _is_exportable(e)]
    excluded = len(found) - len(evals)
    by_id = {e.id: e for e in evals}
    if not evals:
        return ExportBundle([], {}, missing, excluded)

    res_rows = (
        (await db.execute(select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id.in_(list(by_id)))))
        .scalars()
        .all()
    )
    version_ids = list({e.scorecard_version_id for e in evals})
    nodes = (
        (await db.execute(select(KpiNode).where(KpiNode.scorecard_version_id.in_(version_ids)))).scalars().all()
    )
    parent_ids = {n.parent_id for n in nodes if n.parent_id is not None}
    leaf_ids = [n.id for n in nodes if n.id not in parent_ids]
    guide: dict[uuid.UUID, dict[int, str]] = {}
    if leaf_ids:
        g_rows = (await db.execute(select(KpiGuideline).where(KpiGuideline.kpi_node_id.in_(leaf_ids)))).scalars().all()
        for g in g_rows:
            guide.setdefault(g.kpi_node_id, {})[int(g.score_level)] = g.qualitative_text

    nodes_by_version: dict[uuid.UUID, list[KpiNode]] = {}
    for n in nodes:
        nodes_by_version.setdefault(n.scorecard_version_id, []).append(n)
    kpis_by_version = {vid: build_kpis(ns, guide) for vid, ns in nodes_by_version.items()}
    for vid in version_ids:
        kpis_by_version.setdefault(vid, [])

    results_by_eval: dict[uuid.UUID, dict[uuid.UUID, ExportResult]] = {}
    for r in res_rows:
        results_by_eval.setdefault(r.evaluation_id, {})[r.kpi_node_id] = ExportResult(
            kpi_id=r.kpi_node_id,
            score=float(r.score),
            matched_level=r.matched_guideline_level,
            reasoning=r.reasoning_text or "",
            evidence=normalize_evidence(r.evidence_quotes),
            needs_review=bool(r.needs_review),
            variance=float(r.score_variance) if r.score_variance is not None else None,
            ensemble=_ensemble_text(r.ensemble_raw_scores),
        )

    out: list[ExportEvaluation] = []
    for e in evals:
        version = e.scorecard_version
        card = version.scorecard
        kpis = kpis_by_version[e.scorecard_version_id]
        results = results_by_eval.get(e.id, {})
        status = e.status.value if hasattr(e.status, "value") else str(e.status)
        out.append(
            ExportEvaluation(
                id=e.id, name=e.name, subject_name=e.subject_name, subject_email=e.subject_email,
                scorecard_id=card.id, scorecard_name=card.name, version_id=version.id,
                version_number=version.version_number, domain=e.domain or card.domain, status=status,
                score=round(float(e.final_weighted_score), 2) if e.final_weighted_score is not None else None,
                target=float(card.target_score) if card.target_score is not None else None,
                evaluator_name=e.evaluator.name if e.evaluator else "", source=e.source_kind or "manual",
                submitted_at=e.submitted_at, finished_at=e.finished_at,
                error=(e.error_message or e.error_code) if status == EvaluationStatus.FAILED.value else None,
                custom_formula=bool(version.scoring_formula), results=results,
                category_scores=rollup_categories(kpis, results),
                kpis_total=sum(1 for k in kpis if k.is_leaf),
            )
        )
    return ExportBundle(out, kpis_by_version, missing, excluded)
