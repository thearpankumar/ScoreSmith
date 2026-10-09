"""In-app notifications: a Postgres row per (user, event). Creation is idempotent through the per-user
`dedupe_key` unique index (`INSERT .. ON CONFLICT DO NOTHING`), so a retried job or request never notifies twice.

Two entry points: `add_notification(db, ...)` joins the caller's transaction (API handlers - the notification
commits atomically with the change it announces), `notify_background(...)` opens its own short session for the
worker and NEVER raises (a notification must not break a pipeline)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification

logger = logging.getLogger(__name__)

# Event catalogue (docs/plan-sharing-rbac.md).
INVITE_RECEIVED = "invite_received"
INVITE_ACCEPTED = "invite_accepted"
INVITE_DECLINED = "invite_declined"
INVITE_REVOKED = "invite_revoked"
COLLABORATOR_REMOVED = "collaborator_removed"
COLLABORATOR_LEFT = "collaborator_left"
CHART_EDITED = "chart_edited"
EVALUATION_COMPLETED = "evaluation_completed"
EVALUATION_FAILED = "evaluation_failed"
BATCH_COMPLETED = "batch_completed"
SCORECARD_SAVED = "scorecard_saved"
CHAT_QUESTION = "chat_question"
CHAT_SHARED = "chat_shared"
CHART_SHARED = "chart_shared"  # added to a chart directly by an administrator (no invitation)
CHART_TRASHED = "chart_trashed"
CHART_RESTORED = "chart_restored"
EVALUATION_RESUMED = "evaluation_resumed"
EVALUATION_RETRY_AVAILABLE = "evaluation_retry_available"


async def add_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    type_: str,
    title: str,
    *,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    link: str | None = None,
    dedupe_key: str | None = None,
) -> bool:
    """Adds the notification to `db`'s transaction. Returns False when `dedupe_key` already existed for the user."""
    stmt = (
        pg_insert(Notification)
        .values(
            id=uuid.uuid4(), user_id=user_id, type=type_, title=title[:300], body=body, data=data, link=link,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing()
        .returning(Notification.id)
    )
    return (await db.execute(stmt)).first() is not None


async def notify_background(user_id: uuid.UUID, type_: str, title: str, **kwargs: Any) -> None:
    try:
        from app.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await add_notification(db, user_id, type_, title, **kwargs)
            await db.commit()
    except Exception:  # noqa: BLE001 - a notification must never break the job that produced it
        logger.warning("could not create %s notification for %s", type_, user_id, exc_info=True)
