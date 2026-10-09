from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import idempotency as idem
from app.ai.bedrock_client import BedrockClientProtocol, BedrockUnavailableError
from app.ai.judge import compute_final_score, effective_leaf_weights, leaf_nodes, run_judge
from app.audit import audit
from app.authz import accessible_evaluations, get_accessible_evaluation, get_owned_version
from app.config import get_settings
from app.db import get_db
from app.deps import get_bedrock_client, get_current_user
from app.models.enums import AuditAction, EvaluationStatus, rag_band_for_score
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_node import KpiNode
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.schemas.evaluation import (
    PIPELINE_STATUSES,
    EvaluationCreate,
    EvaluationKpiResultCreate,
    EvaluationKpiResultRead,
    EvaluationKpiResultUpdate,
    EvaluationRead,
    EvaluationReadWithResults,
    EvaluationUpdate,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


class EvaluationRunRequest(BaseModel):
    input_text: str


@router.post("/{evaluation_id}/run", response_model=EvaluationReadWithResults)
async def run_evaluation(
    evaluation_id: uuid.UUID,
    payload: EvaluationRunRequest,
    db: AsyncSession = Depends(get_db),
    bedrock: BedrockClientProtocol = Depends(get_bedrock_client),
    current_user: User = Depends(get_current_user),
) -> Evaluation:
    """Runs the LLM judge (see app/ai/judge.py) against `payload.input_text` for every
    leaf KPI under this evaluation's scorecard version, and persists per-KPI results plus
    the weighted final score / RAG band onto the existing `evaluations` /
    `evaluation_kpi_results` rows."""
    evaluation = await get_accessible_evaluation(db, current_user, evaluation_id)

    # Keep a record of what was actually judged (input_reference is documented as
    # free-form JSONB metadata about the evaluated input — see data_dictionary.md).
    evaluation.input_reference = {**(evaluation.input_reference or {}), "text": payload.input_text}
    await db.flush()

    settings = get_settings()
    try:
        await run_judge(
            db, evaluation, payload.input_text, bedrock, judge_model_id=settings.bedrock_judge_model_id
        )
    except BedrockUnavailableError as exc:
        evaluation.status = EvaluationStatus.FAILED
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except ValueError as exc:
        evaluation.status = EvaluationStatus.FAILED
        await db.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    result = await db.execute(
        select(Evaluation)
        .where(Evaluation.id == evaluation_id)
        .options(selectinload(Evaluation.kpi_results))
    )
    return result.scalar_one()


@router.post("/{evaluation_id}/finalize", response_model=EvaluationReadWithResults)
async def finalize_evaluation(
    evaluation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Evaluation:
    """Manual-scoring finalize step: computes `final_weighted_score`/`rag_band` from
    whatever `EvaluationKpiResult` rows already exist for this evaluation (posted one per
    leaf KPI via `POST /{id}/results` — see the "Score manually" flow in
    `frontend/components/chart-detail/EvaluateTab.tsx`), via the SAME
    `app/ai/judge.py::compute_final_score` the AI judge path (`POST /{id}/run` -> `run_judge`
    above) uses — honoring the scorecard version's custom `scoring_formula` if it has one,
    falling back byte-for-byte to the classic weighted average otherwise. This is the one
    shared server-side computation both scoring paths go through, so they can never
    silently disagree about what a scorecard's score means (previously the manual path
    computed this client-side and PATCHed the result directly — see git history/task notes
    for this pass; that client-side math is retained as a live preview only now, this
    endpoint is the actual source of truth persisted).
    """
    evaluation = await get_accessible_evaluation(db, current_user, evaluation_id)

    nodes_result = await db.execute(
        select(KpiNode).where(KpiNode.scorecard_version_id == evaluation.scorecard_version_id)
    )
    nodes = list(nodes_result.scalars().all())
    leaves = leaf_nodes(nodes)
    if not leaves:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"scorecard_version {evaluation.scorecard_version_id} has no KPI nodes to finalize against.",
        )

    results_result = await db.execute(
        select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == evaluation_id)
    )
    # `EvaluationKpiResult.score` is a Numeric column — psycopg returns it as
    # `decimal.Decimal`, which `simpleeval`'s arithmetic (via app/ai/scoring_formula.py)
    # cannot mix with plain floats (e.g. a literal `0.5` in a custom formula). Coerce to
    # float here, the one place manually-submitted scores cross into that shared math —
    # the AI judge path never hits this since `KpiJudgment.score` is already a Python
    # float (from `statistics.median`).
    scores_by_kpi = {r.kpi_node_id: float(r.score) for r in results_result.scalars().all()}
    missing = [leaf.name for leaf in leaves if leaf.id not in scores_by_kpi]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing a score for {len(missing)} KPI(s): {', '.join(missing)}",
        )

    weights = effective_leaf_weights(nodes)
    version = await db.get(ScorecardVersion, evaluation.scorecard_version_id)
    scoring_formula = version.scoring_formula if version is not None else None
    try:
        final_score = compute_final_score(leaves, scores_by_kpi, weights, scoring_formula)
    except ValueError as exc:
        evaluation.status = EvaluationStatus.FAILED
        await db.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    evaluation.final_weighted_score = round(final_score, 2)
    evaluation.rag_band = rag_band_for_score(final_score)
    evaluation.status = EvaluationStatus.COMPLETED
    evaluation.submitted_at = datetime.now(UTC)
    audit(db, request, actor_id=current_user.id, entity_type="evaluation", entity_id=evaluation_id,
          action=AuditAction.UPDATE, event="finalized")
    await db.commit()

    result = await db.execute(
        select(Evaluation)
        .where(Evaluation.id == evaluation_id)
        .options(selectinload(Evaluation.kpi_results))
    )
    return result.scalar_one()


@router.get("", response_model=list[EvaluationRead])
async def list_evaluations(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    scorecard_version_id: uuid.UUID | None = None,
    evaluated_by: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Evaluation]:
    # Only the caller's evaluations and those of scorecards they own; `evaluated_by` just narrows further.
    stmt = accessible_evaluations(current_user).order_by(Evaluation.created_at.desc())
    if scorecard_version_id is not None:
        stmt = stmt.where(Evaluation.scorecard_version_id == scorecard_version_id)
    if evaluated_by is not None:
        stmt = stmt.where(Evaluation.evaluated_by == evaluated_by)
    result = await db.execute(stmt.offset(skip).limit(limit))
    return list(result.scalars().all())


@router.post("", response_model=EvaluationRead, status_code=status.HTTP_201_CREATED)
async def create_evaluation(
    payload: EvaluationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Evaluation:
    key = idem.normalize_key(idempotency_key)
    if key:  # a retried POST with the same key returns the evaluation created the first time
        stored = await idem.begin(
            db, current_user.id, "evaluation", key, idem.request_fingerprint(payload.model_dump_json())
        )
        if stored is not None:
            existing = await db.get(Evaluation, uuid.UUID(stored["evaluation_id"]))
            if existing is not None:
                return existing
    await get_owned_version(db, current_user, payload.scorecard_version_id)  # may only evaluate one's own scorecards
    # The evaluator / owner is always the authenticated user, never a client-supplied id.
    evaluation = Evaluation(**payload.model_dump(), evaluated_by=current_user.id, owner_id=current_user.id)
    db.add(evaluation)
    try:
        await db.flush()
        audit(db, request, actor_id=current_user.id, entity_type="evaluation", entity_id=evaluation.id,
              action=AuditAction.CREATE)
        if key:
            await idem.remember(db, current_user.id, "evaluation", key, {"evaluation_id": str(evaluation.id)})
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(evaluation)
    return evaluation


@router.get("/{evaluation_id}", response_model=EvaluationReadWithResults)
async def get_evaluation(
    evaluation_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> Evaluation:
    result = await db.execute(
        accessible_evaluations(current_user)
        .where(Evaluation.id == evaluation_id)
        .options(selectinload(Evaluation.kpi_results))
    )
    evaluation = result.scalar_one_or_none()
    if evaluation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Evaluation not found.")
    return evaluation


@router.patch("/{evaluation_id}", response_model=EvaluationRead)
async def update_evaluation(
    evaluation_id: uuid.UUID,
    payload: EvaluationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Evaluation:
    evaluation = await get_accessible_evaluation(db, current_user, evaluation_id)
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and evaluation.status in PIPELINE_STATUSES:  # in the queue / running: cancel it instead
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="The evaluation is being processed; cancel it before changing its status."
        )
    for field, value in changes.items():
        setattr(evaluation, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(evaluation)
    return evaluation


@router.delete("/{evaluation_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_evaluation(
    evaluation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    evaluation = await get_accessible_evaluation(db, current_user, evaluation_id)
    await db.delete(evaluation)
    audit(db, request, actor_id=current_user.id, entity_type="evaluation", entity_id=evaluation_id,
          action=AuditAction.DELETE)
    await db.commit()


# --- Evaluation KPI results, nested under an evaluation ---


@router.get("/{evaluation_id}/results", response_model=list[EvaluationKpiResultRead])
async def list_evaluation_kpi_results(
    evaluation_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[EvaluationKpiResult]:
    await get_accessible_evaluation(db, current_user, evaluation_id)
    result = await db.execute(
        select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == evaluation_id)
    )
    return list(result.scalars().all())


@router.post(
    "/{evaluation_id}/results",
    response_model=EvaluationKpiResultRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_evaluation_kpi_result(
    evaluation_id: uuid.UUID,
    payload: EvaluationKpiResultCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EvaluationKpiResult:
    evaluation = await get_accessible_evaluation(db, current_user, evaluation_id)
    # The scored KPI must be a node of the evaluation's own scorecard version.
    node = await db.get(KpiNode, payload.kpi_node_id)
    if node is None or node.scorecard_version_id != evaluation.scorecard_version_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="KPI node not found.")
    kpi_result = EvaluationKpiResult(evaluation_id=evaluation_id, **payload.model_dump())
    db.add(kpi_result)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(kpi_result)
    return kpi_result


@router.patch("/{evaluation_id}/results/{result_id}", response_model=EvaluationKpiResultRead)
async def update_evaluation_kpi_result(
    evaluation_id: uuid.UUID,
    result_id: uuid.UUID,
    payload: EvaluationKpiResultUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EvaluationKpiResult:
    await get_accessible_evaluation(db, current_user, evaluation_id)
    kpi_result = await db.get(EvaluationKpiResult, result_id)
    if kpi_result is None or kpi_result.evaluation_id != evaluation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Evaluation KPI result not found.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(kpi_result, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc.orig)) from exc
    await db.refresh(kpi_result)
    return kpi_result


@router.delete(
    "/{evaluation_id}/results/{result_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_evaluation_kpi_result(
    evaluation_id: uuid.UUID,
    result_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    await get_accessible_evaluation(db, current_user, evaluation_id)
    kpi_result = await db.get(EvaluationKpiResult, result_id)
    if kpi_result is None or kpi_result.evaluation_id != evaluation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Evaluation KPI result not found.")
    await db.delete(kpi_result)
    await db.commit()
