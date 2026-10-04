"""Best-effort `evaluation_events` writer (clone of `app/ai/turn_events.py`): own short-lived session
per call, NEVER raises."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


async def emit_evaluation_event(evaluation_id: uuid.UUID | str, event_type: str, message: str) -> None:
    try:
        from app.db import AsyncSessionLocal
        from app.models.evaluation_event import EvaluationEvent

        async with AsyncSessionLocal() as db:
            db.add(
                EvaluationEvent(
                    evaluation_id=uuid.UUID(str(evaluation_id)),
                    event_type=event_type,
                    message=message[:2000],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - a UX-only trace must never break the pipeline
        logger.warning("emit_evaluation_event failed (%s %s); continuing.", evaluation_id, event_type, exc_info=True)
