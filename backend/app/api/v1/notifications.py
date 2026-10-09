"""Notification inbox. Postgres is the system of record; clients poll `GET /notifications/unread-count` (cheap: one
partial-index count + conditional request, so an unchanged inbox answers `304` with no body) and open the full list
only when the panel is shown. Every query is scoped to the caller. Works with any number of API replicas (no
per-process state)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models.notification import Notification
from app.models.sharing import ScorecardInvitation
from app.models.user import User
from app.ratelimit import rate_limit
from app.schemas.sharing import NotificationPage, NotificationRead, UnreadCount
from app.slots import user_slots

router = APIRouter(prefix="/notifications", tags=["notifications"])
slots_router = APIRouter(prefix="/me", tags=["me"])

_poll_limit = rate_limit("notifications", lambda s: "240/minute", per_user=True)


def _encode(created_at: datetime, nid: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{nid}".encode()).decode()


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, _, nid = raw.partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(nid)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid cursor.") from exc


async def _unread(db: AsyncSession, user: User) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user.id, Notification.read_at.is_(None)
            )
        )
    ).scalar_one()


@router.get("/unread-count", response_model=UnreadCount, dependencies=[Depends(_poll_limit)])
async def unread_count(
    request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    row = (
        await db.execute(
            select(
                func.count().filter(Notification.read_at.is_(None)),
                func.max(Notification.created_at),
                func.count(),
            ).where(Notification.user_id == user.id)
        )
    ).one()
    unread, latest, total = int(row[0]), row[1], int(row[2])
    etag = '"' + hashlib.sha1(f"{unread}:{latest}:{total}".encode()).hexdigest()[:20] + '"'  # noqa: S324
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return UnreadCount(unread=unread)


@router.get("", response_model=NotificationPage)
async def list_notifications(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    unread_only: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NotificationPage:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    if cursor:
        ts, nid = _decode(cursor)
        stmt = stmt.where(
            or_(Notification.created_at < ts, and_(Notification.created_at == ts, Notification.id < nid))
        )
    rows = (
        await db.execute(stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1))
    ).scalars().all()
    page = rows[:limit]
    invite_ids = [
        uuid.UUID(n.data["invitation_id"]) for n in page if n.type == "invite_received" and n.data
        and n.data.get("invitation_id")
    ]
    statuses: dict[uuid.UUID, str] = {}
    if invite_ids:
        statuses = dict(
            (
                await db.execute(
                    select(ScorecardInvitation.id, ScorecardInvitation.status).where(
                        ScorecardInvitation.id.in_(invite_ids), ScorecardInvitation.invitee_id == user.id
                    )
                )
            ).all()
        )
    items = [
        NotificationRead(
            id=n.id, type=n.type, title=n.title, body=n.body, data=n.data, link=n.link, created_at=n.created_at,
            read_at=n.read_at,
            invitation_status=(
                statuses.get(uuid.UUID(n.data["invitation_id"])) if n.type == "invite_received" and n.data
                and n.data.get("invitation_id") else None
            ),
        )
        for n in page
    ]
    return NotificationPage(
        items=items,
        next_cursor=_encode(page[-1].created_at, page[-1].id) if len(rows) > limit else None,
        unread=await _unread(db, user),
    )


@router.post("/read-all", response_model=UnreadCount)
async def mark_all_read(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> UnreadCount:
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return UnreadCount(unread=0)


@router.post("/{notification_id}/read", response_model=UnreadCount)
async def mark_read(
    notification_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> UnreadCount:
    result = await db.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user.id)
        .values(read_at=func.coalesce(Notification.read_at, func.now()))
        .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
        .returning(Notification.id)
    )
    if result.first() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    await db.commit()
    return UnreadCount(unread=await _unread(db, user))


@slots_router.get("/slots")
async def my_slots(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """The caller's busy slots (one running evaluation job, one running chat turn), so the UI can disable the
    run / send buttons and link to the blocking job."""
    return await user_slots(db, user.id)
