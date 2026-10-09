"""Keyset-paginated, server-side-filtered Evaluations list (infinite scroll), plus the "all matching the filter" bulk
delete and the id refresh used for live progress badges.

Pagination: stable sort `(sort_key, id)`; the cursor carries the last row's key + id and the next page seeks with a
row-value comparison (`(key, id) < (:k, :id)`), so rows inserted while scrolling can never duplicate or shift a page.
Totals are COUNTs over the same filters, capped (`COUNT_CAP`) so a huge result never makes the list slow.

Registered BEFORE `evaluations.router` so `/evaluations/page` is never captured by `/evaluations/{evaluation_id}`."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import Select, and_, delete, exists, func, literal, not_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.audit import audit
from app.authz import scorecard_access_clause
from app.db import get_db
from app.deps import get_current_user
from app.models.enums import AuditAction, EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import ScorecardCollaborator
from app.models.user import User
from app.ratelimit import rate_limit
from app.schemas.evaluation_page import (
    BulkDeleteResult,
    BulkSelection,
    EvalFilter,
    EvalListItem,
    EvalPage,
    EvalRefreshRequest,
    SortKey,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations-page"])

COUNT_CAP = 10_000
FINISHED = (EvaluationStatus.COMPLETED, EvaluationStatus.FAILED)
Runner = aliased(User)

_SORTS: dict[str, tuple[Any, bool]] = {
    "newest": (Evaluation.created_at, True),
    "oldest": (Evaluation.created_at, False),
    "score_desc": (func.coalesce(Evaluation.final_weighted_score, -1), True),
    "score_asc": (func.coalesce(Evaluation.final_weighted_score, -1), False),
    "name_asc": (func.lower(Evaluation.name), False),
    "name_desc": (func.lower(Evaluation.name), True),
}


def _from(stmt: Select, user: User) -> Select:
    return (
        stmt.select_from(Evaluation)
        .join(ScorecardVersion, ScorecardVersion.id == Evaluation.scorecard_version_id)
        .join(Scorecard, Scorecard.id == ScorecardVersion.scorecard_id)
        .join(Runner, Runner.id == Evaluation.owner_id)
        .where(scorecard_access_clause(user))
    )


def _like(text_: str) -> str:
    return "%" + text_.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def apply_filters(stmt: Select, user: User, f: EvalFilter) -> Select:
    if f.status == "active":
        stmt = stmt.where(Evaluation.status.not_in(FINISHED))
    elif f.status == "completed":
        stmt = stmt.where(Evaluation.status == EvaluationStatus.COMPLETED)
    elif f.status == "failed":
        stmt = stmt.where(Evaluation.status == EvaluationStatus.FAILED)
    if f.scorecard_id:
        stmt = stmt.where(ScorecardVersion.scorecard_id == f.scorecard_id)
    if f.batch_id:
        stmt = stmt.where(Evaluation.batch_id == f.batch_id)
    if f.band:
        stmt = stmt.where(Evaluation.rag_band.in_(f.band))
    if f.min_score is not None:
        stmt = stmt.where(Evaluation.final_weighted_score >= f.min_score)
    if f.max_score is not None:
        stmt = stmt.where(Evaluation.final_weighted_score <= f.max_score)
    if f.meets_target is True:
        stmt = stmt.where(
            Scorecard.target_score.is_not(None), Evaluation.final_weighted_score >= Scorecard.target_score
        )
    elif f.meets_target is False:
        stmt = stmt.where(
            Scorecard.target_score.is_not(None),
            Evaluation.final_weighted_score.is_not(None),
            Evaluation.final_weighted_score < Scorecard.target_score,
        )
    if f.date_from:
        stmt = stmt.where(Evaluation.created_at >= datetime.combine(f.date_from, time.min, tzinfo=UTC))
    if f.date_to:
        stmt = stmt.where(Evaluation.created_at < datetime.combine(f.date_to, time.min, tzinfo=UTC) + timedelta(days=1))
    if f.q and f.q.strip():
        pat = _like(f.q.strip())
        stmt = stmt.where(
            or_(
                Evaluation.name.ilike(pat, escape="\\"),
                Evaluation.subject_name.ilike(pat, escape="\\"),
                Evaluation.subject_email.ilike(pat, escape="\\"),
                Scorecard.name.ilike(pat, escape="\\"),
                Runner.name.ilike(pat, escape="\\"),
            )
        )
    if f.runner:
        if f.runner == "me":
            stmt = stmt.where(Evaluation.owner_id == user.id)
        elif f.runner == "others":
            stmt = stmt.where(Evaluation.owner_id != user.id)
        else:
            try:
                stmt = stmt.where(Evaluation.owner_id == uuid.UUID(f.runner))
            except ValueError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid runner filter.") from exc
    if f.shared is not None:
        has_collab = exists().where(ScorecardCollaborator.scorecard_id == Scorecard.id)
        stmt = stmt.where(has_collab if f.shared else not_(has_collab))
    return stmt


def filter_from_query(
    status_: str = Query(default="all", alias="status"),
    scorecard_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    band: list[str] = Query(default_factory=list),
    min_score: float | None = Query(default=None, ge=0, le=10),
    max_score: float | None = Query(default=None, ge=0, le=10),
    meets_target: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    runner: str | None = None,
    shared: bool | None = None,
) -> EvalFilter:
    try:
        return EvalFilter.model_validate(
            {
                "status": status_, "scorecard_id": scorecard_id, "batch_id": batch_id, "band": band,
                "min_score": min_score, "max_score": max_score, "meets_target": meets_target,
                "date_from": date_from or None, "date_to": date_to or None, "q": q, "runner": runner,
                "shared": shared,
            }
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


def _encode(sort: str, key: Any, eid: uuid.UUID) -> str:
    if isinstance(key, datetime):
        key = key.isoformat()
    elif isinstance(key, Decimal):
        key = str(key)
    return base64.urlsafe_b64encode(json.dumps({"s": sort, "k": key, "i": str(eid)}).encode()).decode()


def _decode(sort: str, cursor: str) -> tuple[Any, uuid.UUID]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if raw["s"] != sort:
            raise ValueError("cursor belongs to a different sort")
        key: Any = raw["k"]
        if sort in ("newest", "oldest"):
            key = datetime.fromisoformat(key)
        elif sort.startswith("score"):
            key = Decimal(str(key))
        return key, uuid.UUID(raw["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid cursor.") from exc


def _item(row, user: User) -> EvalListItem:
    ev, sc_id, sc_name, target, runner_name, shared = row[:6]
    return EvalListItem(
        id=ev.id, name=ev.name, status=ev.status.value, stage=ev.stage,
        final_weighted_score=float(ev.final_weighted_score) if ev.final_weighted_score is not None else None,
        rag_band=ev.rag_band.value if ev.rag_band else None, created_at=ev.created_at, submitted_at=ev.submitted_at,
        queued_at=ev.queued_at, started_at=ev.started_at, finished_at=ev.finished_at,
        subject_name=ev.subject_name, subject_email=ev.subject_email, batch_id=ev.batch_id,
        error_code=ev.error_code, error_message=ev.error_message, attempt=ev.attempt or 1, domain=ev.domain,
        scorecard_id=sc_id, scorecard_version_id=ev.scorecard_version_id, scorecard_name=sc_name,
        target_score=float(target) if target is not None else None, runner_id=ev.owner_id,
        runner_name=runner_name, is_mine=ev.owner_id == user.id, shared=bool(shared),
    )


def _columns(sort_expr: Any) -> list[Any]:
    return [
        Evaluation, Scorecard.id, Scorecard.name, Scorecard.target_score, Runner.name,
        exists().where(ScorecardCollaborator.scorecard_id == Scorecard.id).label("shared"),
        sort_expr.label("sort_key"),
    ]


@router.get("/page", response_model=EvalPage)
async def evaluations_page(
    cursor: str | None = None,
    limit: int = Query(default=40, ge=1, le=100),
    sort: SortKey = "newest",
    flt: EvalFilter = Depends(filter_from_query),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EvalPage:
    sort_expr, desc = _SORTS[sort]
    stmt = apply_filters(_from(select(*_columns(sort_expr)), user), user, flt)
    if cursor:
        key, eid = _decode(sort, cursor)
        pair, bound = tuple_(sort_expr, Evaluation.id), tuple_(literal(key, sort_expr.type), literal(eid))
        stmt = stmt.where(pair < bound if desc else pair > bound)
    order = (sort_expr.desc(), Evaluation.id.desc()) if desc else (sort_expr.asc(), Evaluation.id.asc())
    rows = (await db.execute(stmt.order_by(*order).limit(limit + 1))).all()
    page = rows[:limit]

    cols = select(Evaluation.status.label("st"), Evaluation.final_weighted_score.label("sc"))
    capped = apply_filters(_from(cols, user), user, flt).limit(COUNT_CAP + 1).subquery()
    total, selectable, exportable = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(capped.c.st.in_(FINISHED)),
                func.count().filter(and_(capped.c.st == EvaluationStatus.COMPLETED, capped.c.sc.is_not(None))),
            ).select_from(capped)
        )
    ).one()
    return EvalPage(
        items=[_item(r, user) for r in page],
        next_cursor=_encode(sort, page[-1][6], page[-1][0].id) if len(rows) > limit else None,
        total=min(int(total), COUNT_CAP), selectable=int(selectable), exportable=int(exportable),
        total_capped=int(total) > COUNT_CAP, count_cap=COUNT_CAP,
    )


@router.post("/refresh", response_model=list[EvalListItem])
async def refresh_rows(
    payload: EvalRefreshRequest, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[EvalListItem]:
    """Current state of up to 100 rows (live progress badges); ids the caller may not see are simply absent."""
    stmt = _from(select(*_columns(Evaluation.created_at)), user).where(Evaluation.id.in_(payload.ids))
    return [_item(r, user) for r in (await db.execute(stmt)).all()]


async def resolve_selection(db: AsyncSession, user: User, sel: BulkSelection, *, cap: int) -> list[uuid.UUID]:
    """Ids a bulk action applies to: access-checked, in the page's default order, minus `exclude_ids`."""
    stmt = _from(select(Evaluation.id), user)
    if sel.ids is not None:
        stmt = stmt.where(Evaluation.id.in_(sel.ids))
    else:
        stmt = apply_filters(stmt, user, sel.filter)  # type: ignore[arg-type]
    if sel.exclude_ids:
        stmt = stmt.where(Evaluation.id.not_in(sel.exclude_ids))
    rows = (await db.execute(stmt.order_by(Evaluation.created_at.desc(), Evaluation.id.desc()).limit(cap))).all()
    return [r[0] for r in rows]


@router.post("/bulk-delete", response_model=BulkDeleteResult, dependencies=[
    Depends(rate_limit("bulk_delete", lambda s: s.rate_limit_export, per_user=True))])
async def bulk_delete(
    payload: BulkSelection,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BulkDeleteResult:
    """Deletes finished (completed / failed) evaluations - the explicit ids, or everything matching the filter (server
    side select-all). Running ones are skipped and reported. Results / sources / events cascade in the database."""
    from app.schemas.evaluation_page import MAX_BULK_DELETE

    ids = await resolve_selection(db, user, payload, cap=MAX_BULK_DELETE)
    finished = (
        await db.execute(select(Evaluation.id).where(Evaluation.id.in_(ids), Evaluation.status.in_(FINISHED)))
    ).scalars().all() if ids else []
    for start in range(0, len(finished), 500):
        await db.execute(delete(Evaluation).where(Evaluation.id.in_(finished[start:start + 500])))
    audit(db, request, actor_id=user.id, entity_type="evaluation_bulk", entity_id=user.id,
          action=AuditAction.DELETE, event="bulk_delete", diff={"count": len(finished)})
    await db.commit()
    return BulkDeleteResult(deleted=len(finished), skipped=len(ids) - len(finished))
