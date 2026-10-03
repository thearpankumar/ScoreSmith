from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.bedrock_client import BedrockClientProtocol, BedrockUnavailableError
from app.ai.scoring_formula import validate as validate_scoring_formula_expr
from app.ai.similarity import SIMILARITY_THRESHOLD, SimilarScorecardResult, find_similar_scorecards
from app.db import get_db
from app.deps import get_bedrock_client, get_current_user
from app.models.evaluation import Evaluation
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.schemas.scorecard import (
    ScorecardCreate,
    ScorecardRead,
    ScorecardUpdate,
    ScorecardVersionCreate,
    ScorecardVersionRead,
    ScorecardVersionReadWithKpiNodes,
    ScorecardVersionUpdate,
)

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
) -> list[SimilarScorecardResult]:
    """Free-text query -> ranked existing scorecards worth reusing/adapting (the "suggest
    similar scorecard" feature from the plan). Embeds `query` with Titan, then runs a
    pgvector cosine-similarity search over `scorecard_embeddings`."""
    try:
        return await find_similar_scorecards(
            db, bedrock, payload.query, top_n=payload.top_n, threshold=payload.threshold
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
async def validate_formula_draft_endpoint(payload: ValidateFormulaDraftRequest) -> ValidateFormulaDraftResponse:
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
    skip: int = 0,
    limit: int = 100,
    owner_id: uuid.UUID | None = None,
    domain: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[Scorecard]:
    stmt = select(Scorecard).order_by(Scorecard.created_at.desc())
    if owner_id is not None:
        stmt = stmt.where(Scorecard.owner_id == owner_id)
    if domain is not None:
        stmt = stmt.where(Scorecard.domain == domain)
    result = await db.execute(stmt.offset(skip).limit(limit))
    return list(result.scalars().all())


@router.post("", response_model=ScorecardRead, status_code=status.HTTP_201_CREATED)
async def create_scorecard(
    payload: ScorecardCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> Scorecard:
    scorecard = Scorecard(**payload.model_dump())
    db.add(scorecard)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(scorecard)
    return scorecard


@router.get("/{scorecard_id}", response_model=ScorecardRead)
async def get_scorecard(scorecard_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Scorecard:
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard not found.")
    return scorecard


@router.patch("/{scorecard_id}", response_model=ScorecardRead)
async def update_scorecard(
    scorecard_id: uuid.UUID,
    payload: ScorecardUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> Scorecard:
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard not found.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(scorecard, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(scorecard)
    return scorecard


@router.delete("/{scorecard_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_scorecard(
    scorecard_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> None:
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard not found.")
    # Deleting a scorecard deletes "every version of it, and its evaluations" (the delete dialog's
    # promise). Evaluations reference a version with ON DELETE RESTRICT, and their per-KPI results
    # reference KPI nodes, so they have to go first; deleting an evaluation cascades to its results.
    version_ids = select(ScorecardVersion.id).where(ScorecardVersion.scorecard_id == scorecard_id)
    evaluations = (
        await db.execute(select(Evaluation).where(Evaluation.scorecard_version_id.in_(version_ids)))
    ).scalars().all()
    for evaluation in evaluations:
        await db.delete(evaluation)
    await db.flush()
    # Break the current_version_id circular reference first so the version can cascade-delete.
    scorecard.current_version_id = None
    await db.flush()
    await db.delete(scorecard)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Cannot delete scorecard: still referenced elsewhere: {exc.orig}"
        ) from exc


# --- Scorecard versions, nested under a scorecard ---


@router.get("/{scorecard_id}/versions", response_model=list[ScorecardVersionRead])
async def list_scorecard_versions(
    scorecard_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[ScorecardVersion]:
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> ScorecardVersion:
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard not found.")
    version = ScorecardVersion(scorecard_id=scorecard_id, **payload.model_dump())
    db.add(version)
    try:
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
    scorecard_id: uuid.UUID, version_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ScorecardVersion:
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
) -> ValidateFormulaResponse:
    """Real backend validation for the chart-detail formula editor's live green/red
    indicator (see app/ai/scoring_formula.py::validate) — the frontend calls this rather
    than reimplementing formula parsing client-side, so validation can never drift from
    what `PATCH .../versions/{id}` (below) or an actual evaluation would do."""
    version = await db.get(ScorecardVersion, version_id)
    if version is None or version.scorecard_id != scorecard_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard version not found.")
    names = await _leaf_kpi_names(db, version_id)
    result = validate_scoring_formula_expr(payload.formula, names)
    return ValidateFormulaResponse(**result.to_dict())


@router.patch("/{scorecard_id}/versions/{version_id}", response_model=ScorecardVersionRead)
async def update_scorecard_version(
    scorecard_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ScorecardVersionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> ScorecardVersion:
    version = await db.get(ScorecardVersion, version_id)
    if version is None or version.scorecard_id != scorecard_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard version not found.")
    data = payload.model_dump(exclude_unset=True)
    if "scoring_formula" in data and data["scoring_formula"]:
        names = await _leaf_kpi_names(db, version_id)
        result = validate_scoring_formula_expr(data["scoring_formula"], names)
        if not result.valid:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result.error)
    for field, value in data.items():
        setattr(version, field, value)
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> None:
    version = await db.get(ScorecardVersion, version_id)
    if version is None or version.scorecard_id != scorecard_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard version not found.")
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is not None and scorecard.current_version_id == version_id:
        scorecard.current_version_id = None
        await db.flush()
    await db.delete(version)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Cannot delete version: referenced elsewhere (e.g. by an evaluation): {exc.orig}",
        ) from exc
