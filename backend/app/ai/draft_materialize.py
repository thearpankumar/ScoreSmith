"""Turns a confirmed `ScorecardDraft` (from the chat builder) into real
`Scorecard` / `ScorecardVersion` / `KpiNode` / `KpiGuideline` rows via the existing
SQLAlchemy models — the same ones `app/api/v1/scorecards.py` and `kpi_nodes.py` use, so
a materialized draft is indistinguishable from a scorecard built by hand through the
Cycle 1b CRUD API.

`path`/`level` computation intentionally mirrors
`app/api/v1/kpi_nodes.py::_compute_path_and_level` (node id hex as the ltree label,
level derived from the parent's actual level rather than trusted from the draft) rather
than importing that router module, to keep the AI layer independent of the API layer.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy_utils import Ltree

from app.ai.draft_schema import ScorecardDraft
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion


def _label_for(node_id: uuid.UUID) -> str:
    return node_id.hex


_AUDIENCE_PREFIX = "Audience: "


def draft_from_scorecard(
    scorecard: Scorecard, nodes: list[KpiNode], scoring_formula: str | None = None
) -> ScorecardDraft:
    """Inverse of `materialize_draft`: turns an existing scorecard's current version (its
    KPI nodes + guidelines, already loaded) into a `ScorecardDraft`, so a chat session can
    start from it ("Refine with assistant") instead of from an empty draft.

    The draft references parents by *name* (see draft_schema.py), so duplicate KPI names
    within one scorecard (legal in the DB, ambiguous in the draft) are disambiguated with
    a numeric suffix rather than silently merged."""
    ordered = sorted(nodes, key=lambda n: (n.level, n.display_order, str(n.path)))
    name_by_id: dict[uuid.UUID, str] = {}
    used: set[str] = set()
    for node in ordered:
        base = node.name.strip() or "Unnamed KPI"
        candidate, n = base, 2
        while candidate in used:
            candidate = f"{base} ({n})"
            n += 1
        used.add(candidate)
        name_by_id[node.id] = candidate

    kpis = []
    for node in ordered:
        kpis.append(
            {
                "name": name_by_id[node.id],
                "weight": float(node.weight),
                "level": node.level,
                "parent_name": name_by_id.get(node.parent_id) if node.parent_id else None,
                "included_in_scoring": bool(node.included_in_scoring),
                "guidelines": {
                    str(g.score_level): {
                        "qualitative_text": g.qualitative_text,
                        "quantitative_criteria": g.quantitative_criteria,
                    }
                    for g in node.guidelines
                },
            }
        )

    # scorecards.scope <- "Audience: {audience}" on materialization; undo that here.
    audience = scorecard.scope
    if audience and audience.startswith(_AUDIENCE_PREFIX):
        audience = audience[len(_AUDIENCE_PREFIX):]

    return ScorecardDraft.model_validate(
        {
            "name": scorecard.name,
            "purpose": scorecard.purpose_statement,
            "domain": scorecard.domain,
            "audience": audience,
            "target_score": float(scorecard.target_score) if scorecard.target_score is not None else None,
            "kpis": kpis,
            "scoring_formula": scoring_formula,
        }
    )


async def materialize_draft(
    db: AsyncSession,
    draft: ScorecardDraft,
    owner_id: uuid.UUID,
    existing_scorecard_id: uuid.UUID | None = None,
) -> tuple[Scorecard, ScorecardVersion]:
    """Persists a confirmed draft. Raises if the draft is not complete
    (`ScorecardDraft.is_complete()`), since an incomplete draft would fail the DB's own
    weight-sum trigger or leave KPIs without guidelines.

    - `existing_scorecard_id=None` (the default, unchanged behavior): creates a brand-new
      Scorecard at version 1.
    - `existing_scorecard_id` set (a "Refine with assistant" session, or a second save
      from the same chat): appends a NEW version (max version_number + 1) to that
      scorecard, makes it the current version, and updates the scorecard's header fields
      from the draft. Earlier versions — and every evaluation that references them — are
      left untouched, so past results stay reproducible."""
    if not draft.is_complete():
        raise ValueError(f"Cannot materialize an incomplete draft: missing {draft.missing_fields()}")

    # ScorecardDraft has no dedicated `scope` field (see draft_schema.py module docstring
    # for the audience -> scorecards.scope mapping rationale).
    scope_parts: list[str] = []
    if draft.audience:
        scope_parts.append(f"{_AUDIENCE_PREFIX}{draft.audience}")
    scope = "\n\n".join(scope_parts) or None

    scorecard: Scorecard | None = None
    if existing_scorecard_id is not None:
        scorecard = await db.get(Scorecard, existing_scorecard_id)

    if scorecard is None:
        scorecard = Scorecard(
            name=draft.name or "Untitled Scorecard",
            owner_id=owner_id,
            domain=draft.domain,
            purpose_statement=draft.purpose,
            scope=scope,
            target_score=draft.target_score,
        )
        db.add(scorecard)
        await db.flush()  # assigns scorecard.id
        version_number = 1
    else:
        scorecard.name = draft.name or scorecard.name
        scorecard.domain = draft.domain
        scorecard.purpose_statement = draft.purpose
        scorecard.scope = scope
        scorecard.target_score = draft.target_score
        max_version = await db.scalar(
            select(func.max(ScorecardVersion.version_number)).where(
                ScorecardVersion.scorecard_id == scorecard.id
            )
        )
        version_number = (max_version or 0) + 1
        previous = await db.execute(
            select(ScorecardVersion).where(
                ScorecardVersion.scorecard_id == scorecard.id, ScorecardVersion.is_active.is_(True)
            )
        )
        for old in previous.scalars():
            old.is_active = False

    version = ScorecardVersion(
        scorecard_id=scorecard.id,
        version_number=version_number,
        created_by=owner_id,
        is_active=True,
        scoring_formula=draft.scoring_formula,
    )
    db.add(version)
    await db.flush()  # assigns version.id

    # Precompute ids so children can reference a parent's id/path before that parent row
    # is committed. Process in level order (root-first) so a parent's path is always
    # known by the time a child needs it, mirroring the CRUD API's incremental behavior.
    node_ids: dict[str, uuid.UUID] = {kpi.name: uuid.uuid4() for kpi in draft.kpis}
    path_by_name: dict[str, str] = {}
    level_by_name: dict[str, int] = {}

    for kpi in sorted(draft.kpis, key=lambda k: k.level):
        node_id = node_ids[kpi.name]
        if kpi.parent_name is None:
            path = _label_for(node_id)
            level = 1
        else:
            parent_path = path_by_name[kpi.parent_name]
            parent_level = level_by_name[kpi.parent_name]
            if parent_level >= 4:
                raise ValueError(f"KPI {kpi.name!r} would exceed max hierarchy depth of 4.")
            path = f"{parent_path}.{_label_for(node_id)}"
            level = parent_level + 1

        path_by_name[kpi.name] = path
        level_by_name[kpi.name] = level

        node = KpiNode(
            id=node_id,
            scorecard_version_id=version.id,
            parent_id=node_ids[kpi.parent_name] if kpi.parent_name else None,
            path=Ltree(path),
            level=level,
            name=kpi.name,
            weight=kpi.weight if kpi.weight is not None else 0,
            display_order=0,
            included_in_scoring=kpi.included_in_scoring,
        )
        db.add(node)

        for score_level_str, guideline in kpi.guidelines.items():
            db.add(
                KpiGuideline(
                    kpi_node_id=node_id,
                    score_level=int(score_level_str),
                    qualitative_text=guideline.qualitative_text,
                    quantitative_criteria=guideline.quantitative_criteria,
                )
            )

    scorecard.current_version_id = version.id
    await db.commit()  # single commit: the deferred weight-sum trigger validates here
    await db.refresh(scorecard)
    await db.refresh(version)
    return scorecard, version
