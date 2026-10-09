"""What happens when an AI evaluation reaches a terminal state: the runner's notification and the chart's activity
log entry. Called by whichever process drove the evaluation (a worker), and by the API for user cancels.

Idempotent: notifications use a per-user `dedupe_key`; log entries are checked by (action, evaluation, attempt), so a
re-driven job (worker crash + adoption) or a retried request never produces a second notification or log line."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import notifications as notif
from app.activity import notify_editors, record_activity
from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import ScorecardActivity
from app.models.user import User

logger = logging.getLogger(__name__)

_TERMINAL = (EvaluationStatus.COMPLETED, EvaluationStatus.FAILED)


async def _activity_exists(db: AsyncSession, scorecard_id: uuid.UUID, action: str, key: str) -> bool:
    # Serialise concurrent finalisers of the same evaluation / batch (released on commit): no duplicate lines.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"activity:{action}:{key}"})
    return (
        await db.execute(
            select(func.count())
            .select_from(ScorecardActivity)
            .where(
                ScorecardActivity.scorecard_id == scorecard_id,
                ScorecardActivity.action == action,
                ScorecardActivity.detail["key"].astext == key,
            )
        )
    ).scalar_one() > 0


async def record_evaluation_outcome(db: AsyncSession, ev: Evaluation) -> None:
    """Adds the activity-log line (and, outside batches, the runner's notification) for a terminal evaluation."""
    if ev.status not in _TERMINAL:
        return
    version = await db.get(ScorecardVersion, ev.scorecard_version_id)
    if version is None:
        return
    scorecard = await db.get(Scorecard, version.scorecard_id)
    runner = await db.get(User, ev.owner_id)
    if scorecard is None:
        return
    cancelled = ev.status == EvaluationStatus.FAILED and ev.error_code == "cancelled"
    if ev.status == EvaluationStatus.COMPLETED:
        action = "evaluation_completed"
        score = f"{float(ev.final_weighted_score):.1f}" if ev.final_weighted_score is not None else "n/a"
        summary = f"Evaluation “{ev.name}” completed (score {score})"
    elif cancelled:
        action, summary = "evaluation_cancelled", f"Evaluation “{ev.name}” was cancelled"
    else:
        action, summary = "evaluation_failed", f"Evaluation “{ev.name}” failed"
    key = f"{ev.id}:{ev.attempt}"
    # Batch members are summarised by one "batch completed" line / notification instead of N of each.
    if ev.batch_id is None and not await _activity_exists(db, scorecard.id, action, key):
        record_activity(
            db, scorecard.id, runner, action, summary, entity_type="evaluation", entity_id=ev.id,
            detail={"key": key, "status": ev.status.value, "score": float(ev.final_weighted_score)
                    if ev.final_weighted_score is not None else None, "error_code": ev.error_code},
        )
        if runner is not None:
            await notify_editors(db, scorecard.id, runner, action)  # no-op for non-edit actions
    if ev.batch_id is None and not cancelled and runner is not None:
        failed = ev.status == EvaluationStatus.FAILED
        await notif.add_notification(
            db, ev.owner_id,
            notif.EVALUATION_FAILED if failed else notif.EVALUATION_COMPLETED,
            f"Evaluation failed: {ev.name}" if failed else f"Evaluation complete: {ev.name}",
            body=(ev.error_message or "The evaluation could not be completed.")[:300] if failed
            else f"Score {float(ev.final_weighted_score):.1f} on “{scorecard.name}”."
            if ev.final_weighted_score is not None else f"Finished on “{scorecard.name}”.",
            data={"evaluation_id": str(ev.id), "scorecard_id": str(scorecard.id), "status": ev.status.value},
            link=f"/charts/{scorecard.id}/evaluations/{ev.id}",
            dedupe_key=f"eval:{ev.id}:{ev.attempt}:{ev.status.value}",
        )


async def record_batch_outcome(db: AsyncSession, batch_id: uuid.UUID) -> None:
    """Once every evaluation of the batch is finished: one notification to its creator + one log line."""
    batch = await db.get(EvaluationBatch, batch_id)
    if batch is None:
        return
    rows = (await db.execute(select(Evaluation.status).where(Evaluation.batch_id == batch_id))).scalars().all()
    if not rows or any(s not in _TERMINAL for s in rows):
        return
    done = sum(1 for s in rows if s == EvaluationStatus.COMPLETED)
    failed = len(rows) - done
    scorecard = await db.get(Scorecard, batch.scorecard_id)
    creator = await db.get(User, batch.created_by)
    if scorecard is None:
        return
    # `attempt` of retried members changes the signature, so a retried-then-finished batch is announced again.
    attempts = (await db.execute(select(func.coalesce(func.sum(Evaluation.attempt), 0)).where(
        Evaluation.batch_id == batch_id))).scalar_one()
    key = f"{batch_id}:{attempts}"
    if not await _activity_exists(db, scorecard.id, "batch_completed", key):
        record_activity(
            db, scorecard.id, creator, "batch_completed",
            f"Batch of {len(rows)} evaluations finished ({done} completed, {failed} failed)",
            entity_type="evaluation_batch", entity_id=batch_id,
            detail={"key": key, "completed": done, "failed": failed, "total": len(rows)},
        )
    if creator is not None:
        await notif.add_notification(
            db, creator.id, notif.BATCH_COMPLETED, f"Batch finished on {scorecard.name}",
            body=f"{done} of {len(rows)} evaluations completed" + (f", {failed} failed." if failed else "."),
            data={"batch_id": str(batch_id), "scorecard_id": str(scorecard.id), "completed": done, "failed": failed},
            link=f"/evaluations?batch={batch_id}", dedupe_key=f"batch:{key}",
        )


async def finalize_evaluation_background(eid: uuid.UUID) -> None:
    """Worker hook (own session, never raises): outcome of one evaluation + batch roll-up."""
    try:
        async with AsyncSessionLocal() as db:
            ev = await db.get(Evaluation, eid)
            if ev is None:
                return
            await record_evaluation_outcome(db, ev)
            if ev.batch_id is not None:
                await record_batch_outcome(db, ev.batch_id)
            await db.commit()
    except Exception:  # noqa: BLE001 - never break the pipeline over a notification
        logger.warning("could not record the outcome of evaluation %s", eid, exc_info=True)
