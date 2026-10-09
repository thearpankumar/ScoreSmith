"""Per-user access checks. Every endpoint that takes a resource id loads it through one of these helpers.

Each helper queries with the access rule in the WHERE clause and answers 404 - never 403 - on a miss, so a caller
cannot tell "someone else's" from "does not exist". (The single exception: a collaborator who may SEE a chart but
tries an owner-only action such as delete / manage sharing gets 403 `owner_only`; they already know it exists.)

Access model (one place - `scorecard_access_clause`):
- scorecard                : the OWNER (`scorecards.owner_id`) or an accepted COLLABORATOR (`scorecard_collaborators`,
                             role "editor"); versions, KPI nodes, guidelines through their scorecard. A pending
                             invitee has NO access except the read-only preview endpoint. Admins have no bypass.
- evaluation               : follows its scorecard (owner or collaborator); the runner is only a label / job-slot owner
- batch                    : follows its scorecard
- trash                    : a chart with `deleted_at` set matches none of the above (404) - only the owner's trash
                             endpoints (app/trash.py) can see it
- chat session             : strictly private to `chat_sessions.user_id`
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import ScorecardCollaborator
from app.models.user import User


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{what} not found.")


def scorecard_access_clause(user: User):
    """WHERE-clause (on `Scorecard`) for charts the user may open: owned or shared with them (accepted)."""
    return and_(
        Scorecard.deleted_at.is_(None),  # a trashed chart is invisible to everybody (see app/trash.py)
        or_(
            Scorecard.owner_id == user.id,
            Scorecard.id.in_(
                select(ScorecardCollaborator.scorecard_id).where(ScorecardCollaborator.user_id == user.id)
            ),
        ),
    )


def owner_only() -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={"code": "owner_only", "message": "Only the owner of this chart can do that."},
    )


async def get_accessible_scorecard(db: AsyncSession, user: User, scorecard_id: uuid.UUID) -> Scorecard:
    scorecard = (
        await db.execute(select(Scorecard).where(Scorecard.id == scorecard_id, scorecard_access_clause(user)))
    ).scalar_one_or_none()
    if scorecard is None:
        raise _not_found("Scorecard")
    return scorecard


async def get_owned_scorecard(db: AsyncSession, user: User, scorecard_id: uuid.UUID) -> Scorecard:
    """Owner-only actions (delete, manage sharing). A collaborator gets 403 `owner_only`, anyone else 404."""
    scorecard = await get_accessible_scorecard(db, user, scorecard_id)
    if scorecard.owner_id != user.id:
        raise owner_only()
    return scorecard


async def scorecard_is_shared(db: AsyncSession, scorecard_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(ScorecardCollaborator.id).where(ScorecardCollaborator.scorecard_id == scorecard_id).limit(1)
        )
    ).first() is not None


async def get_accessible_version(
    db: AsyncSession, user: User, version_id: uuid.UUID, scorecard_id: uuid.UUID | None = None
) -> ScorecardVersion:
    stmt = (
        select(ScorecardVersion)
        .join(Scorecard, Scorecard.id == ScorecardVersion.scorecard_id)
        .where(ScorecardVersion.id == version_id, scorecard_access_clause(user))
    )
    if scorecard_id is not None:
        stmt = stmt.where(ScorecardVersion.scorecard_id == scorecard_id)
    version = (await db.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise _not_found("Scorecard version")
    return version


async def get_accessible_kpi_node(db: AsyncSession, user: User, node_id: uuid.UUID) -> KpiNode:
    node = (
        await db.execute(
            select(KpiNode)
            .join(ScorecardVersion, ScorecardVersion.id == KpiNode.scorecard_version_id)
            .join(Scorecard, Scorecard.id == ScorecardVersion.scorecard_id)
            .where(KpiNode.id == node_id, scorecard_access_clause(user))
        )
    ).scalar_one_or_none()
    if node is None:
        raise _not_found("KPI node")
    return node


async def get_accessible_guideline(
    db: AsyncSession, user: User, node_id: uuid.UUID, guideline_id: uuid.UUID
) -> KpiGuideline:
    node = await get_accessible_kpi_node(db, user, node_id)
    guideline = await db.get(KpiGuideline, guideline_id)
    if guideline is None or guideline.kpi_node_id != node.id:
        raise _not_found("Guideline")
    return guideline


def evaluation_access_clause(user: User):
    """WHERE-clause for evaluations the user may see: every evaluation of a chart they own or collaborate on
    (evaluations are shared among collaborators; the runner is only a label). Requires `Evaluation` joined to
    `ScorecardVersion` and `Scorecard` (see `accessible_evaluations`)."""
    return scorecard_access_clause(user)


def accessible_evaluations(user: User):
    return (
        select(Evaluation)
        .join(ScorecardVersion, ScorecardVersion.id == Evaluation.scorecard_version_id)
        .join(Scorecard, Scorecard.id == ScorecardVersion.scorecard_id)
        .where(evaluation_access_clause(user))
    )


async def get_accessible_evaluation(db: AsyncSession, user: User, evaluation_id: uuid.UUID) -> Evaluation:
    evaluation = (
        await db.execute(accessible_evaluations(user).where(Evaluation.id == evaluation_id))
    ).scalar_one_or_none()
    if evaluation is None:
        raise _not_found("Evaluation")
    return evaluation


async def accessible_evaluation_ids(db: AsyncSession, user: User, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """The subset of `ids` the user may access (order preserved)."""
    if not ids:
        return []
    rows = (
        (await db.execute(accessible_evaluations(user).where(Evaluation.id.in_(ids)).with_only_columns(Evaluation.id)))
        .scalars()
        .all()
    )
    allowed = set(rows)
    return [i for i in ids if i in allowed]


async def get_accessible_batch(db: AsyncSession, user: User, batch_id: uuid.UUID) -> EvaluationBatch:
    batch = (
        await db.execute(
            select(EvaluationBatch)
            .join(Scorecard, Scorecard.id == EvaluationBatch.scorecard_id)
            .where(EvaluationBatch.id == batch_id, scorecard_access_clause(user))
        )
    ).scalar_one_or_none()
    if batch is None:
        raise _not_found("Batch")
    return batch


async def get_owned_chat_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> ChatSession:
    session = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id))
    ).scalar_one_or_none()
    if session is None:
        raise _not_found("Chat session")
    return session
