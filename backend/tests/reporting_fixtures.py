"""Synthetic `ExportBundle` builders for the pure (no database) export tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.reporting.export_data import (
    EvidenceItem,
    ExportBundle,
    ExportEvaluation,
    ExportKpi,
    ExportResult,
    rollup_categories,
)

# (category, kpi name, weight)
DEFAULT_LEAVES = [
    ("Communication", "Clarity", 30),
    ("Communication", "Listening", 20),
    ("Technical", "Depth", 30),
    ("Technical", "Edge cases", 20),
]


def make_version(leaves=DEFAULT_LEAVES) -> tuple[uuid.UUID, list[ExportKpi]]:
    vid = uuid.uuid4()
    kpis: list[ExportKpi] = []
    cats: dict[str, ExportKpi] = {}
    for cat, name, weight in leaves:
        if cat not in cats:
            c = ExportKpi(
                id=uuid.uuid4(), parent_id=None, name=cat, level=1, weight=None, included=True, is_leaf=False,
                order=len(kpis), path=(cat,),
            )
            cats[cat] = c
            kpis.append(c)
        kpis.append(
            ExportKpi(
                id=uuid.uuid4(), parent_id=cats[cat].id, name=name, level=2, weight=float(weight), included=True,
                is_leaf=True, order=len(kpis), path=(cat, name), category=cat,
                guidelines={0: "Absent", 5: "Adequate", 10: "Outstanding"},
            )
        )
    return vid, kpis


def make_eval(
    vid: uuid.UUID, kpis: list[ExportKpi], name: str, email: str | None, score: float | None, leaf_scores: list[float],
    *, scorecard: str = "Interview Scorecard", scorecard_id: uuid.UUID | None = None, status: str = "completed",
    target: float | None = None, needs_review: bool = False, evidence: list[EvidenceItem] | None = None,
) -> ExportEvaluation:
    leaves = [k for k in kpis if k.is_leaf]
    results = {
        k.id: ExportResult(
            kpi_id=k.id, score=s, matched_level=round(s), reasoning=f"Reason for {k.name}",
            evidence=evidence if evidence is not None else [EvidenceItem(f"quote {k.name}", "Section 2")],
            needs_review=needs_review and i == 0, variance=0.5 if needs_review and i == 0 else None, ensemble="7, 8, 8",
        )
        for i, (k, s) in enumerate(zip(leaves, leaf_scores, strict=False))
    }
    return ExportEvaluation(
        id=uuid.uuid4(), name=f"{name} – interview", subject_name=name, subject_email=email,
        scorecard_id=scorecard_id or uuid.uuid5(uuid.NAMESPACE_DNS, scorecard), scorecard_name=scorecard, version_id=vid,
        version_number=1, domain="Hiring", status=status, score=score, target=target, evaluator_name="Eve Evaluator",
        source="upload", submitted_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 10, 1, 9, 30, tzinfo=UTC), error=None, custom_formula=False, results=results,
        category_scores=rollup_categories(kpis, results), kpis_total=len(leaves),
    )


def make_bundle(rows: list[tuple[str, str | None, float | None, list[float]]] | None = None, **kw) -> ExportBundle:
    """rows: (name, email, overall score, leaf scores in DEFAULT_LEAVES order)."""
    vid, kpis = make_version()
    rows = rows or [
        ("Priya Shah", "priya@example.com", 9.1, [9, 9, 9, 9]),
        ("Rahul Kumar", "rahul@example.com", 3.4, [4, 3, 3, 3]),
        ("Asha Menon", "asha@example.com", 7.5, [8, 7, 8, 7]),
        ("Dev Patel", None, 7.5, [7, 8, 7, 8]),
        ("Mia Chen", "mia@example.com", 5.2, [6, 5, 5, 5]),
        ("Omar Ali", "omar@example.com", 8.2, [8, 8, 9, 8]),
    ]
    evals = [make_eval(vid, kpis, n, e, s, ls, **kw) for n, e, s, ls in rows]
    return ExportBundle(evaluations=evals, kpis_by_version={vid: kpis})
