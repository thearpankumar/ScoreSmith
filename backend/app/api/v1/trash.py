"""The owner's chart trash: list, restore, purge, empty. Strictly per user - these endpoints only ever see charts the
caller OWNS and has trashed; a collaborator (or admin) never sees someone else's trash, and an id that is not in the
caller's trash behaves like a missing one. Registered BEFORE `/scorecards/{scorecard_id}` so the literal path wins.
See docs/plan-sharing-rbac.md ("Trash")."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import trash
from app.audit import audit
from app.db import get_db
from app.deps import get_current_user
from app.models.enums import AuditAction
from app.models.scorecard import Scorecard
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scorecards/trash", tags=["trash"])

MAX_IDS = 200


class TrashItem(BaseModel):
    id: uuid.UUID
    name: str
    domain: str | None
    deleted_at: datetime
    days_left: int
    collaborator_count: int
    evaluation_count: int


class TrashIds(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_IDS)


class TrashResult(BaseModel):
    """`done` = ids processed; `not_found` = ids that are not in the caller's trash (reported alike for foreign ids)."""

    done: list[uuid.UUID]
    not_found: list[uuid.UUID]


def _own_trash(user: User):
    return select(Scorecard).where(Scorecard.owner_id == user.id, Scorecard.deleted_at.is_not(None))


async def _lazy_purge(db: AsyncSession) -> None:
    try:
        await trash.purge_expired(db)
    except Exception:  # noqa: BLE001 - listing the trash must not fail because housekeeping did
        await db.rollback()
        logger.warning("lazy trash purge failed", exc_info=True)


@router.get("", response_model=list[TrashItem])
async def list_trash(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[TrashItem]:
    await _lazy_purge(db)
    cards = (await db.execute(_own_trash(user).order_by(Scorecard.deleted_at.desc()))).scalars().all()
    collab, evals = await trash.trash_counts(db, [c.id for c in cards])
    now = datetime.now(UTC)
    return [
        TrashItem(
            id=c.id, name=c.name, domain=c.domain, deleted_at=c.deleted_at,
            days_left=trash.days_left(c.deleted_at, now),
            collaborator_count=collab.get(c.id, 0), evaluation_count=evals.get(c.id, 0),
        )
        for c in cards
    ]


async def _load(db: AsyncSession, user: User, ids: list[uuid.UUID]) -> tuple[list[Scorecard], list[uuid.UUID]]:
    wanted = list(dict.fromkeys(ids))
    cards = (
        await db.execute(
            _own_trash(user).where(Scorecard.id.in_(wanted)).order_by(Scorecard.deleted_at).with_for_update()
        )
    ).scalars().all()
    found = {c.id for c in cards}
    missing = [i for i in wanted if i not in found]
    if not cards:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Nothing to do: none of these charts is in your trash.")
    return list(cards), missing


@router.post("/restore", response_model=TrashResult)
async def restore(
    payload: TrashIds, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TrashResult:
    cards, missing = await _load(db, user, payload.ids)
    for card in cards:
        await trash.restore_from_trash(db, user, card)
        audit(db, request, actor_id=user.id, entity_type="scorecard", entity_id=card.id,
              action=AuditAction.UPDATE, event="chart_restored")
    await db.commit()
    from app.pipeline.dispatcher import get_dispatcher

    get_dispatcher().wake()  # jobs re-queued by the restore start without waiting for the next poll
    return TrashResult(done=[c.id for c in cards], not_found=missing)


async def _purge(db: AsyncSession, request: Request, user: User, cards: list[Scorecard]) -> None:
    for card in cards:
        audit(db, request, actor_id=user.id, entity_type="scorecard", entity_id=card.id,
              action=AuditAction.DELETE, event="chart_purged")
        await trash.delete_scorecard_cascade(db, card)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Cannot delete the chart(s): still referenced elsewhere: {exc.orig}"
        ) from exc


@router.post("/purge", response_model=TrashResult)
async def purge(
    payload: TrashIds, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TrashResult:
    """Permanent delete of the chosen trashed charts (versions, evaluations and sharing go with them)."""
    cards, missing = await _load(db, user, payload.ids)
    done = [c.id for c in cards]
    await _purge(db, request, user, cards)
    return TrashResult(done=done, not_found=missing)


@router.post("/empty", response_model=TrashResult)
async def empty(
    request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TrashResult:
    """Permanent delete of everything in the caller's trash (an empty trash answers 200 with nothing done)."""
    cards = list((await db.execute(_own_trash(user).with_for_update())).scalars().all())
    done = [c.id for c in cards]
    if cards:
        await _purge(db, request, user, cards)
    return TrashResult(done=done, not_found=[])
