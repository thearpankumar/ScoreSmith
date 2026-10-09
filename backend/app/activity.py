"""Append-only editing log of a chart (`scorecard_activity`), plus the coalesced "X edited" notification to the
other collaborators. Rows join the caller's transaction, so a change and its log entry commit (or roll back) together.
Recording is always on (cheap); the UI shows the panel only for shared charts."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import notifications as notif
from app.models.scorecard import Scorecard
from app.models.sharing import ScorecardActivity, ScorecardCollaborator
from app.models.user import User

logger = logging.getLogger(__name__)

# Edit-type actions that notify the other collaborators (at most one per actor / chart / 10 minutes).
_NOTIFY_ACTIONS = {
    "scorecard_updated", "version_created", "version_updated", "version_deleted", "kpi_added", "kpi_updated",
    "kpi_deleted", "weights_changed", "guideline_added", "guideline_updated", "guideline_deleted",
}
_BUCKET_SECONDS = 600


def record_activity(
    db: AsyncSession,
    scorecard_id: uuid.UUID,
    actor: User | None,
    action: str,
    summary: str,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        ScorecardActivity(
            scorecard_id=scorecard_id,
            actor_id=actor.id if actor else None,
            actor_name=(actor.name if actor else "Someone")[:200],
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary[:1000],
            detail=detail,
        )
    )


async def notify_editors(db: AsyncSession, scorecard_id: uuid.UUID, actor: User, action: str) -> None:
    """Coalesced "chart edited" notification to the owner + collaborators other than `actor` (shared charts only)."""
    if action not in _NOTIFY_ACTIONS:
        return
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is None:
        return
    others = set(
        (
            await db.execute(
                select(ScorecardCollaborator.user_id).where(ScorecardCollaborator.scorecard_id == scorecard_id)
            )
        )
        .scalars()
        .all()
    )
    if not others:
        return
    others.add(scorecard.owner_id)
    others.discard(actor.id)
    bucket = int(time.time() // _BUCKET_SECONDS)
    for uid in others:
        await notif.add_notification(
            db, uid, notif.CHART_EDITED, f"{actor.name} edited “{scorecard.name}”",
            body="See the editing log on the chart for details.",
            data={"scorecard_id": str(scorecard_id), "actor_id": str(actor.id)},
            link=f"/charts/{scorecard_id}",
            dedupe_key=f"edit:{scorecard_id}:{actor.id}:{bucket}",
        )


async def log_edit(
    db: AsyncSession,
    scorecard_id: uuid.UUID,
    actor: User,
    action: str,
    summary: str,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    record_activity(
        db, scorecard_id, actor, action, summary, entity_type=entity_type, entity_id=entity_id, detail=detail
    )
    await notify_editors(db, scorecard_id, actor, action)
