"""Per-user concurrency slots: at most ONE running evaluation job and ONE running chat turn per user.

The slot is DERIVED from rows that already carry their own lease / terminal state, so nothing can leak:
* evaluation job = evaluations with `owner_id = user` in queued / ingesting / processing / scoring (a batch is one job:
  rows sharing a `batch_id`). It frees itself on completion, failure, cancel, deactivation and when a worker dies
  (another worker adopts the expired lease and finishes or fails the row).
* chat turn = a chat session of the user with a non-stale `pending_turn_started_at` (the reaper clears turns whose
  worker lease expired).
Admission is atomic across replicas: the check and the insert/claim happen in ONE transaction that holds a
transaction-scoped Postgres advisory lock keyed by (user, kind), so two concurrent requests serialise and exactly one
is admitted. 409 bodies carry a machine-readable `code` plus the id of the blocking job so the UI can link to it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import STALE_TURN_TIMEOUT_SECONDS, ChatSession
from app.models.enums import ACTIVE_EVALUATION_STATUSES, EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.scorecard_version import ScorecardVersion

JOB_ACTIVE = "user_job_active"
CHAT_ACTIVE = "user_chat_active"

ACTIVE_JOB_STATUSES = (EvaluationStatus.QUEUED, *ACTIVE_EVALUATION_STATUSES)


class UserJobActiveError(Exception):
    def __init__(self, evaluation_id: uuid.UUID, batch_id: uuid.UUID | None, scorecard_id: uuid.UUID | None = None):
        super().__init__("user job active")
        self.evaluation_id, self.batch_id, self.scorecard_id = evaluation_id, batch_id, scorecard_id

    def http(self) -> HTTPException:
        return job_active_http(self.evaluation_id, self.batch_id, self.scorecard_id)


def job_active_http(
    evaluation_id: uuid.UUID, batch_id: uuid.UUID | None, scorecard_id: uuid.UUID | None = None
) -> HTTPException:
    kind = "batch" if batch_id else "evaluation"
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": JOB_ACTIVE,
            "message": (
                f"You already have an {kind} running. Wait for it to finish (or cancel it) before starting another."
            ),
            "evaluation_id": str(evaluation_id),
            "batch_id": str(batch_id) if batch_id else None,
            "scorecard_id": str(scorecard_id) if scorecard_id else None,
        },
    )


async def lock_user_slot(db: AsyncSession, user_id: uuid.UUID, kind: str) -> None:
    """Transaction-scoped advisory lock (released on commit / rollback)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"slot:{kind}:{user_id}"})


async def active_job_row(
    db: AsyncSession, user_id: uuid.UUID, *, same_batch_as: uuid.UUID | None = None
) -> Evaluation | None:
    """The user's oldest active evaluation, ignoring rows of `same_batch_as` (one batch = one job)."""
    stmt = select(Evaluation).where(Evaluation.owner_id == user_id, Evaluation.status.in_(ACTIVE_JOB_STATUSES))
    if same_batch_as is not None:
        stmt = stmt.where((Evaluation.batch_id.is_(None)) | (Evaluation.batch_id != same_batch_as))
    return (await db.execute(stmt.order_by(Evaluation.created_at).limit(1))).scalar_one_or_none()


async def _scorecard_of(db: AsyncSession, ev: Evaluation) -> uuid.UUID | None:
    return (
        await db.execute(select(ScorecardVersion.scorecard_id).where(ScorecardVersion.id == ev.scorecard_version_id))
    ).scalar_one_or_none()


async def acquire_job_slot(db: AsyncSession, user_id: uuid.UUID, *, same_batch_as: uuid.UUID | None = None) -> None:
    """Call inside the transaction that creates / re-queues the job rows; raises 409 when a job is already active."""
    await lock_user_slot(db, user_id, "eval")
    blocking = await active_job_row(db, user_id, same_batch_as=same_batch_as)
    if blocking is not None:
        raise UserJobActiveError(blocking.id, blocking.batch_id, await _scorecard_of(db, blocking))


async def acquire_chat_slot(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """Raises 409 `user_chat_active` when ANOTHER session of the user has a live turn. Same-session reuse is handled
    by the per-session `require_idle` claim."""
    await lock_user_slot(db, user_id, "chat")
    stale_before = datetime.now(UTC) - timedelta(seconds=STALE_TURN_TIMEOUT_SECONDS)
    other = (
        await db.execute(
            select(ChatSession.id).where(
                ChatSession.user_id == user_id,
                ChatSession.id != session_id,
                ChatSession.pending_turn_started_at.is_not(None),
                ChatSession.pending_turn_started_at >= stale_before,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if other is not None:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": CHAT_ACTIVE,
                "message": "The assistant is still working on another chat of yours. Wait for it to finish first.",
                "session_id": str(other),
            },
        )


async def user_slots(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """What the UI needs to disable buttons: the active job / chat turn of the user (or None)."""
    job = await active_job_row(db, user_id)
    stale_before = datetime.now(UTC) - timedelta(seconds=STALE_TURN_TIMEOUT_SECONDS)
    chat = (
        await db.execute(
            select(ChatSession.id).where(
                ChatSession.user_id == user_id,
                ChatSession.pending_turn_started_at.is_not(None),
                ChatSession.pending_turn_started_at >= stale_before,
            ).limit(1)
        )
    ).scalar_one_or_none()
    return {
        "job": (
            {"evaluation_id": str(job.id), "batch_id": str(job.batch_id) if job.batch_id else None,
             "status": job.status.value, "scorecard_id": str(await _scorecard_of(db, job))}
            if job else None
        ),
        "chat": {"session_id": str(chat)} if chat else None,
    }
