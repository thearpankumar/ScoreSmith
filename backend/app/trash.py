"""Chart trash: soft delete, restore, permanent purge and the retention housekeeping.

A trashed chart (`scorecards.deleted_at` set) keeps all its rows but matches no access rule in `app/authz.py`
(404 for the owner and every collaborator) until the owner restores it. Only the OWNER's trash endpoints
(`app/api/v1/trash.py`) can see it. After `TRASH_RETENTION_DAYS` the housekeeping (`purge_expired`, called by the
worker loop and lazily when the trash is listed) deletes it for good.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import notifications as notif
from app.activity import record_activity
from app.config import get_settings
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.evaluation_source import EvaluationSource
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import ScorecardCollaborator
from app.models.user import User
from app.slots import ACTIVE_JOB_STATUSES, active_job_row, lock_user_slot

logger = logging.getLogger(__name__)

PURGE_BATCH = 50
CANCELLED_BY_TRASH = "cancelled_by_trash"  # evaluations.cancel_reason of jobs stopped because their chart was trashed
TRASH_RESTORE_HANDLED = "trash_restore_handled"  # ... after a restore re-queued them (or told the runner to retry)


def retention() -> timedelta:
    return timedelta(days=get_settings().trash_retention_days)


def days_left(deleted_at: datetime, now: datetime | None = None) -> int:
    """Whole days until the automatic purge, rounded up (30 right after trashing, never below 0)."""
    now = now or datetime.now(UTC)
    remaining = (deleted_at + retention() - now).total_seconds()
    return max(0, math.ceil(remaining / 86400))


async def delete_scorecard_cascade(db: AsyncSession, scorecard: Scorecard) -> None:
    """Deleting a scorecard deletes "every version of it, and its evaluations" (the delete dialog's promise).
    Evaluations reference a version with ON DELETE RESTRICT, and their per-KPI results reference KPI nodes, so they
    have to go first; deleting an evaluation cascades to its results. Collaborators / invitations / the activity log
    cascade with the scorecard row."""
    version_ids = select(ScorecardVersion.id).where(ScorecardVersion.scorecard_id == scorecard.id)
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


async def cancel_all_jobs_on_scorecard(scorecard_id: uuid.UUID) -> int:
    """Running / queued evaluations of EVERY runner on the chart are cancelled (via the dispatcher, so the worker
    driving them stops too). Best effort per row; finished rows stay with the chart."""
    from app.db import AsyncSessionLocal
    from app.pipeline.dispatcher import get_dispatcher

    async with AsyncSessionLocal() as db:
        ids = (
            await db.execute(
                select(Evaluation.id)
                .join(ScorecardVersion, ScorecardVersion.id == Evaluation.scorecard_version_id)
                .where(ScorecardVersion.scorecard_id == scorecard_id, Evaluation.status.in_(ACTIVE_JOB_STATUSES))
            )
        ).scalars().all()
    dispatcher = get_dispatcher()
    cancelled = 0
    for eid in ids:
        try:
            await dispatcher.cancel(eid, reason=CANCELLED_BY_TRASH)
            cancelled += 1
        except Exception:  # noqa: BLE001 - already finished meanwhile
            logger.info("could not cancel evaluation %s while trashing its chart", eid)
    return cancelled


async def resume_trash_cancelled_jobs(db: AsyncSession, user: User, scorecard: Scorecard) -> dict[str, int]:
    """Restore side of "trash cancels running jobs": re-queue EXACTLY the evaluations that were cancelled by the trash
    (`cancel_reason = cancelled_by_trash`, still failed/cancelled), never user cancels or ordinary failures.

    Safe by construction:
    * per-user slot: under the runner's slot lock a job is only re-queued when the runner has NO active job; a batch
      counts as one job (its trash-cancelled members all come back together). A runner who already started something
      else is NOT queued behind it (there is no waiting list - the slot IS the row state); they get an
      `evaluation_retry_available` notification and can press Retry on the chart. Deactivated / deleted runners are
      skipped.
    * idempotent: the rows are flipped to `trash_restore_handled` in the same transaction, so a repeated restore, a
      second trash/restore cycle or a retry of the request never re-queues them again; dedupe keys protect the
      notifications.
    * lease/dispatch design unchanged: the rows become ordinary `queued` ones (new attempt, fresh execution name), the
      dispatcher claims them like any retry. The caller commits and wakes the dispatcher.
    Returns {"requeued": n, "retry_offered": n}."""
    rows = (
        await db.execute(
            select(Evaluation)
            .join(ScorecardVersion, ScorecardVersion.id == Evaluation.scorecard_version_id)
            .where(
                ScorecardVersion.scorecard_id == scorecard.id,
                Evaluation.status == EvaluationStatus.FAILED,
                Evaluation.error_code == "cancelled",
                Evaluation.cancel_reason == CANCELLED_BY_TRASH,
            )
            .order_by(Evaluation.queued_at.asc().nulls_last(), Evaluation.created_at)
            .with_for_update(of=Evaluation)
        )
    ).scalars().all()
    by_runner: dict[uuid.UUID, list[Evaluation]] = {}
    for ev in rows:
        by_runner.setdefault(ev.owner_id, []).append(ev)
    now = datetime.now(UTC)
    requeued = offered = 0
    for runner_id, evs in by_runner.items():
        runner = await db.get(User, runner_id)
        first = evs[0]
        job = [e for e in evs if e.batch_id == first.batch_id] if first.batch_id else [first]
        rest = [e for e in evs if e not in job]
        can_run = runner is not None and runner.is_active and runner.deleted_at is None
        if can_run:
            await lock_user_slot(db, runner_id, "eval")
            can_run = await active_job_row(db, runner_id, same_batch_as=first.batch_id) is None
        for ev in job:
            ev.cancel_reason = TRASH_RESTORE_HANDLED
            if not can_run:
                continue
            ev.status = EvaluationStatus.QUEUED
            ev.attempt = (ev.attempt or 1) + 1
            ev.error_code = ev.error_message = ev.sfn_execution_arn = None
            ev.progress = None
            ev.stage = "queued"
            ev.queued_at, ev.started_at, ev.finished_at = now, None, None
            ev.cancel_requested_at = ev.lease_owner = ev.lease_expires_at = None
            await db.execute(
                update(EvaluationSource).where(EvaluationSource.evaluation_id == ev.id)
                .values(status="pending", warnings=None)
            )
        for ev in rest:
            ev.cancel_reason = TRASH_RESTORE_HANDLED
        if job and job[0].batch_id and can_run:
            await db.execute(
                update(EvaluationBatch).where(EvaluationBatch.id == job[0].batch_id).values(status="running")
            )
        if runner is None or not (runner.is_active and runner.deleted_at is None):
            continue
        n = len(evs)
        link = f"/charts/{scorecard.id}"
        if can_run:
            requeued += len(job)
            record_activity(
                db, scorecard.id, user, "evaluations_resumed",
                f"{len(job)} evaluation(s) of {runner.name} were re-queued after the chart was restored",
                entity_type="scorecard", entity_id=scorecard.id,
                detail={"runner_id": str(runner_id), "count": len(job)},
            )
            await notif.add_notification(
                db, runner_id, notif.EVALUATION_RESUMED,
                f"Your evaluation on “{scorecard.name}” was resumed",
                body="It was stopped when the chart went to the trash and has been queued again after the restore.",
                data={"scorecard_id": str(scorecard.id), "evaluation_id": str(first.id)}, link=link,
                dedupe_key=f"resumed:{first.id}:{int(now.timestamp())}",
            )
            if rest:
                offered += len(rest)
        else:
            offered += n
        if not can_run or rest:
            await notif.add_notification(
                db, runner_id, notif.EVALUATION_RETRY_AVAILABLE,
                f"Retry your evaluation on “{scorecard.name}”",
                body="It was stopped when the chart went to the trash. You already have another job running, "
                     "so it was not started automatically - press Retry on the chart when you are ready.",
                data={"scorecard_id": str(scorecard.id), "evaluation_id": str((rest or job)[0].id)}, link=link,
                dedupe_key=f"retry-offer:{(rest or job)[0].id}:{int(now.timestamp())}",
            )
    return {"requeued": requeued, "retry_offered": offered}


async def _collaborator_ids(db: AsyncSession, scorecard_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(
                select(ScorecardCollaborator.user_id).where(ScorecardCollaborator.scorecard_id == scorecard_id)
            )
        ).scalars().all()
    )


async def move_to_trash(db: AsyncSession, user: User, scorecard: Scorecard, now: datetime | None = None) -> None:
    """Owner soft delete. Adds everything to the caller's transaction (the caller commits and then calls
    `cancel_all_jobs_on_scorecard`)."""
    now = now or datetime.now(UTC)
    scorecard.deleted_at = now
    scorecard.deleted_by = user.id
    others = await _collaborator_ids(db, scorecard.id)
    record_activity(
        db, scorecard.id, user, "chart_trashed", f"{user.name} moved the chart to the trash",
        entity_type="scorecard", entity_id=scorecard.id,
    )
    for uid in others:
        await notif.add_notification(
            db, uid, notif.CHART_TRASHED, f"{user.name} moved “{scorecard.name}” to the trash",
            body="The chart is unavailable until the owner restores it.",
            data={"scorecard_id": str(scorecard.id)},
            dedupe_key=f"trashed:{scorecard.id}:{uid}:{int(now.timestamp())}",
        )


async def restore_from_trash(db: AsyncSession, user: User, scorecard: Scorecard) -> None:
    now = datetime.now(UTC)
    scorecard.deleted_at = None
    scorecard.deleted_by = None
    await db.flush()
    await resume_trash_cancelled_jobs(db, user, scorecard)
    record_activity(
        db, scorecard.id, user, "chart_restored", f"{user.name} restored the chart from the trash",
        entity_type="scorecard", entity_id=scorecard.id,
    )
    for uid in await _collaborator_ids(db, scorecard.id):
        await notif.add_notification(
            db, uid, notif.CHART_RESTORED, f"{user.name} restored “{scorecard.name}”",
            body="The chart is available again.", data={"scorecard_id": str(scorecard.id)},
            link=f"/charts/{scorecard.id}",
            dedupe_key=f"restored:{scorecard.id}:{uid}:{int(now.timestamp())}",
        )


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Permanently deletes every chart that has been in the trash longer than the retention. Idempotent and safe to
    run on several replicas at once: rows are claimed with `FOR UPDATE SKIP LOCKED`, so two runners never process the
    same chart and a chart being restored / purged by its owner at that moment is skipped. Commits per batch and
    returns the number of charts removed."""
    now = now or datetime.now(UTC)
    cutoff = now - retention()
    removed = 0
    while True:
        cards = (
            await db.execute(
                select(Scorecard)
                .where(Scorecard.deleted_at.is_not(None), Scorecard.deleted_at <= cutoff)
                .order_by(Scorecard.deleted_at)
                .limit(PURGE_BATCH)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        if not cards:
            await db.commit()
            return removed
        for card in cards:
            await delete_scorecard_cascade(db, card)
        await db.commit()
        removed += len(cards)
        if len(cards) < PURGE_BATCH:
            return removed


async def trash_counts(db: AsyncSession, ids: list[uuid.UUID]) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """(collaborator count, evaluation count) per chart id."""
    if not ids:
        return {}, {}
    collab = dict(
        (
            await db.execute(
                select(ScorecardCollaborator.scorecard_id, func.count())
                .where(ScorecardCollaborator.scorecard_id.in_(ids))
                .group_by(ScorecardCollaborator.scorecard_id)
            )
        ).all()
    )
    evals = dict(
        (
            await db.execute(
                select(ScorecardVersion.scorecard_id, func.count(Evaluation.id))
                .join(Evaluation, Evaluation.scorecard_version_id == ScorecardVersion.id)
                .where(ScorecardVersion.scorecard_id.in_(ids))
                .group_by(ScorecardVersion.scorecard_id)
            )
        ).all()
    )
    return {k: int(v) for k, v in collab.items()}, {k: int(v) for k, v in evals.items()}
