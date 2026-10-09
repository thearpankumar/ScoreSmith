from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.activity import log_edit
from app.ai.bedrock_client import BedrockClientProtocol, BedrockUnavailableError
from app.ai.scoring_formula import validate as validate_scoring_formula_expr
from app.ai.similarity import SIMILARITY_THRESHOLD, SimilarScorecardResult, find_similar_scorecards
from app.api.v1.sharing import leave_chart
from app.audit import audit
from app.authz import (
    get_accessible_scorecard,
    get_accessible_version,
    scorecard_access_clause,
)
from app.db import get_db
from app.deps import get_bedrock_client, get_current_user
from app.models.enums import AuditAction
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import ScorecardCollaborator
from app.models.user import User
from app.occ import check_if_match
from app.schemas.scorecard import (
    ScorecardCreate,
    ScorecardRead,
    ScorecardUpdate,
    ScorecardVersionCreate,
    ScorecardVersionRead,
    ScorecardVersionReadWithKpiNodes,
    ScorecardVersionUpdate,
)
from app.trash import cancel_all_jobs_on_scorecard, delete_scorecard_cascade, move_to_trash  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scorecards", tags=["scorecards"])


class SuggestSimilarRequest(BaseModel):
    query: str
    top_n: int = Field(default=5, ge=1, le=25)
    threshold: float = Field(default=SIMILARITY_THRESHOLD, ge=0, le=1)


class SuggestSimilarResult(BaseModel):
    scorecard_id: uuid.UUID
    scorecard_version_id: uuid.UUID
    name: str
    domain: str | None
    similarity: float
    purpose_statement: str | None = None


@router.post("/suggest-similar", response_model=list[SuggestSimilarResult])
async def suggest_similar_scorecards(
    payload: SuggestSimilarRequest,
    db: AsyncSession = Depends(get_db),
    bedrock: BedrockClientProtocol = Depends(get_bedrock_client),
    current_user: User = Depends(get_current_user),
) -> list[SimilarScorecardResult]:
    """Free-text query -> ranked existing scorecards worth reusing/adapting (the "suggest
    similar scorecard" feature from the plan). Embeds `query` with Titan, then runs a
    pgvector cosine-similarity search over `scorecard_embeddings`."""
    try:
        return await find_similar_scorecards(
            db, bedrock, payload.query, top_n=payload.top_n, threshold=payload.threshold,
            owner_id=current_user.id,
        )
    except BedrockUnavailableError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


class ValidateFormulaDraftRequest(BaseModel):
    formula: str | None = None
    kpi_names: list[str] = []


class ValidateFormulaDraftResponse(BaseModel):
    valid: bool
    error: str | None = None
    unused_kpis: list[str] = []


@router.post("/validate-formula", response_model=ValidateFormulaDraftResponse)
async def validate_formula_draft_endpoint(
    payload: ValidateFormulaDraftRequest, current_user: User = Depends(get_current_user)
) -> ValidateFormulaDraftResponse:
    """Issue 2 (see task notes): the SAME formula-editing capability the chart-detail
    page's `ScoringFormulaPanel`/`ScoringFormulaBuilderDialog` already has, but for a
    chat draft that has no `scorecard_id`/`version_id` yet (it isn't materialized until
    the user confirms — see `app/ai/draft_materialize.py`). Takes the KPI names directly
    (from the in-progress `ScorecardDraft.kpis`, not looked up from a DB row) rather than
    requiring a real version to validate against. Uses the exact same
    `app/ai/scoring_formula.py::validate` as `/{scorecard_id}/versions/{version_id}/
    validate-formula` below, so the two surfaces can never disagree about what's valid —
    see `ScoringFormulaPanel`'s `onValidate` prop, now injected differently by the chat
    live-preview panel vs. the chart-detail Overview tab."""
    result = validate_scoring_formula_expr(payload.formula, payload.kpi_names)
    return ValidateFormulaDraftResponse(**result.to_dict())


@router.get("", response_model=list[ScorecardRead])
async def list_scorecards(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    domain: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Scorecard]:
    stmt = select(Scorecard).where(scorecard_access_clause(current_user)).order_by(Scorecard.created_at.desc())
    if domain is not None:
        stmt = stmt.where(Scorecard.domain == domain)
    result = await db.execute(stmt.offset(skip).limit(limit))
    return await _with_sharing(db, current_user, list(result.scalars().all()))


async def _with_sharing(db: AsyncSession, user: User, cards: list[Scorecard]) -> list[ScorecardRead]:
    """ScorecardRead rows decorated with the caller's role, the owner's name and the collaborator count."""
    if not cards:
        return []
    ids = [c.id for c in cards]
    counts = dict(
        (
            await db.execute(
                select(ScorecardCollaborator.scorecard_id, func.count())
                .where(ScorecardCollaborator.scorecard_id.in_(ids))
                .group_by(ScorecardCollaborator.scorecard_id)
            )
        ).all()
    )
    owners = dict(
        (await db.execute(select(User.id, User.name).where(User.id.in_({c.owner_id for c in cards})))).all()
    )
    out = []
    for c in cards:
        n = int(counts.get(c.id, 0))
        out.append(
            ScorecardRead.model_validate(c).model_copy(
                update={
                    "owner_name": owners.get(c.owner_id),
                    "my_role": "owner" if c.owner_id == user.id else "editor",
                    "is_shared": n > 0,
                    "collaborator_count": n,
                }
            )
        )
    return out


@router.post("", response_model=ScorecardRead, status_code=status.HTTP_201_CREATED)
async def create_scorecard(
    payload: ScorecardCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Scorecard:
    scorecard = Scorecard(**payload.model_dump(), owner_id=current_user.id)  # the owner is always the caller
    db.add(scorecard)
    try:
        await db.flush()
        audit(db, request, actor_id=current_user.id, entity_type="scorecard", entity_id=scorecard.id,
              action=AuditAction.CREATE)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(scorecard)
    return scorecard


@router.get("/{scorecard_id}", response_model=ScorecardRead)
async def get_scorecard(
    scorecard_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> ScorecardRead:
    scorecard = await get_accessible_scorecard(db, current_user, scorecard_id)
    return (await _with_sharing(db, current_user, [scorecard]))[0]


@router.patch("/{scorecard_id}", response_model=ScorecardRead)
async def update_scorecard(
    scorecard_id: uuid.UUID,
    payload: ScorecardUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Scorecard:
    scorecard = await get_accessible_scorecard(db, current_user, scorecard_id)
    check_if_match(request, scorecard, "This chart")
    old_target = scorecard.target_score
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("current_version_id") is not None:  # may only point at one of THIS scorecard's versions
        await get_accessible_version(db, current_user, changes["current_version_id"], scorecard_id)
    for field, value in changes.items():
        setattr(scorecard, field, value)
    try:
        audit(db, request, actor_id=current_user.id, entity_type="scorecard", entity_id=scorecard.id,
              action=AuditAction.UPDATE, diff={"fields": sorted(changes)})
        await log_edit(
            db, scorecard.id, current_user, "scorecard_updated",
            "Changed " + ", ".join(sorted(changes)) if changes else "Saved the chart",
            entity_type="scorecard", entity_id=scorecard.id, detail={"fields": sorted(changes)},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(scorecard)
    if scorecard.target_score != old_target:
        # Scores are coloured against the target at read time (UI and Excel), so nothing needs recomputing.
        logger.info(
            "scorecard target changed: scorecard=%s user=%s old=%s new=%s",
            scorecard.id, current_user.id, old_target, scorecard.target_score,
        )
    return scorecard


@router.delete("/{scorecard_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_scorecard(
    scorecard_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """"Delete" on a chart card. The OWNER moves the chart to the trash (soft delete, restorable for the retention
    period; everybody loses access until it is restored). A COLLABORATOR only removes themselves from the chart
    (the same as `POST .../leave`): the owner and the other collaborators keep it."""
    scorecard = await get_accessible_scorecard(db, current_user, scorecard_id)
    if scorecard.owner_id != current_user.id:
        await leave_chart(db, request, current_user, scorecard)
        return
    await move_to_trash(db, current_user, scorecard)
    audit(db, request, actor_id=current_user.id, entity_type="scorecard", entity_id=scorecard_id,
          action=AuditAction.UPDATE, event="chart_trashed")
    await db.commit()
    await cancel_all_jobs_on_scorecard(scorecard_id)


# --- Scorecard versions, nested under a scorecard ---


@router.get("/{scorecard_id}/versions", response_model=list[ScorecardVersionRead])
async def list_scorecard_versions(
    scorecard_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[ScorecardVersion]:
    await get_accessible_scorecard(db, current_user, scorecard_id)
    result = await db.execute(
        select(ScorecardVersion)
        .where(ScorecardVersion.scorecard_id == scorecard_id)
        .order_by(ScorecardVersion.version_number)
    )
    return list(result.scalars().all())


@router.post(
    "/{scorecard_id}/versions",
    response_model=ScorecardVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_scorecard_version(
    scorecard_id: uuid.UUID,
    payload: ScorecardVersionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScorecardVersion:
    await get_accessible_scorecard(db, current_user, scorecard_id)
    version = ScorecardVersion(scorecard_id=scorecard_id, created_by=current_user.id, **payload.model_dump())
    db.add(version)
    try:
        await db.flush()
        audit(db, request, actor_id=current_user.id, entity_type="scorecard_version", entity_id=version.id,
              action=AuditAction.CREATE)
        await log_edit(
            db, scorecard_id, current_user, "version_created", f"Created version {version.version_number}",
            entity_type="scorecard_version", entity_id=version.id, detail={"version_number": version.version_number},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"version_number must be unique per scorecard: {exc.orig}",
        ) from exc
    await db.refresh(version)
    return version


@router.get(
    "/{scorecard_id}/versions/{version_id}", response_model=ScorecardVersionReadWithKpiNodes
)
async def get_scorecard_version(
    scorecard_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScorecardVersion:
    await get_accessible_version(db, current_user, version_id, scorecard_id)
    result = await db.execute(
        select(ScorecardVersion)
        .where(ScorecardVersion.id == version_id, ScorecardVersion.scorecard_id == scorecard_id)
        .options(selectinload(ScorecardVersion.kpi_nodes).selectinload(KpiNode.guidelines))
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard version not found.")
    return version


async def _leaf_kpi_names(db: AsyncSession, version_id: uuid.UUID) -> list[str]:
    """Leaf KPI display names for a version — the set of variable names a `scoring_formula`
    for it may reference (see app/ai/scoring_formula.py). Only leaves are judged/scored
    (mirrors app/ai/judge.py::leaf_nodes), so an internal grouping node's name is never a
    valid formula variable."""
    result = await db.execute(select(KpiNode).where(KpiNode.scorecard_version_id == version_id))
    nodes = list(result.scalars().all())
    parent_ids = {n.parent_id for n in nodes if n.parent_id is not None}
    return [n.name for n in nodes if n.id not in parent_ids]


class ValidateFormulaRequest(BaseModel):
    formula: str | None = None


class ValidateFormulaResponse(BaseModel):
    valid: bool
    error: str | None = None
    unused_kpis: list[str] = []


@router.post(
    "/{scorecard_id}/versions/{version_id}/validate-formula",
    response_model=ValidateFormulaResponse,
)
async def validate_formula_endpoint(
    scorecard_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ValidateFormulaRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ValidateFormulaResponse:
    """Real backend validation for the chart-detail formula editor's live green/red
    indicator (see app/ai/scoring_formula.py::validate) — the frontend calls this rather
    than reimplementing formula parsing client-side, so validation can never drift from
    what `PATCH .../versions/{id}` (below) or an actual evaluation would do."""
    await get_accessible_version(db, current_user, version_id, scorecard_id)
    names = await _leaf_kpi_names(db, version_id)
    result = validate_scoring_formula_expr(payload.formula, names)
    return ValidateFormulaResponse(**result.to_dict())


@router.patch("/{scorecard_id}/versions/{version_id}", response_model=ScorecardVersionRead)
async def update_scorecard_version(
    scorecard_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ScorecardVersionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScorecardVersion:
    version = await get_accessible_version(db, current_user, version_id, scorecard_id)
    check_if_match(request, version, "This version")
    data = payload.model_dump(exclude_unset=True)
    if "scoring_formula" in data and data["scoring_formula"]:
        names = await _leaf_kpi_names(db, version_id)
        result = validate_scoring_formula_expr(data["scoring_formula"], names)
        if not result.valid:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result.error)
    for field, value in data.items():
        setattr(version, field, value)
    audit(db, request, actor_id=current_user.id, entity_type="scorecard_version", entity_id=version.id,
          action=AuditAction.UPDATE, diff={"fields": sorted(data)})
    await log_edit(
        db, scorecard_id, current_user, "version_updated",
        f"Edited version {version.version_number} ({', '.join(sorted(data)) or 'no changes'})",
        entity_type="scorecard_version", entity_id=version.id, detail={"fields": sorted(data)},
    )
    await db.commit()
    await db.refresh(version)
    return version


@router.delete(
    "/{scorecard_id}/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_scorecard_version(
    scorecard_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    version = await get_accessible_version(db, current_user, version_id, scorecard_id)
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is not None and scorecard.current_version_id == version_id:
        scorecard.current_version_id = None
        await db.flush()
    number = version.version_number
    await db.delete(version)
    try:
        audit(db, request, actor_id=current_user.id, entity_type="scorecard_version", entity_id=version_id,
              action=AuditAction.DELETE)
        await log_edit(
            db, scorecard_id, current_user, "version_deleted", f"Deleted version {number}",
            entity_type="scorecard_version", entity_id=version_id, detail={"version_number": number},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Cannot delete version: referenced elsewhere (e.g. by an evaluation): {exc.orig}",
        ) from exc
