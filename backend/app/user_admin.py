"""Administrative user lifecycle: guards (last admin, self), deactivation side effects and deletion = anonymisation.

Deleting a user NEVER hard-deletes the row (a dozen RESTRICT foreign keys - versions, evaluations, batches - point at
it, and shared charts must keep working). The row becomes an anonymised tombstone:
* no login (no password, `is_active = false`, sessions revoked), name "Deleted user", unique placeholder email,
  no username;
* their PRIVATE charts (no collaborators) and all chats are deleted; charts that are SHARED are handed over to the
  longest-standing collaborator (who stops being a collaborator and becomes the owner), so nothing a team
  relies on vanishes;
* evaluations they ran on other people's charts stay with the chart, labelled "Deleted user";
* collaborator rows, invitations (sent and received), notifications, refresh/reset tokens, OAuth links and
  idempotency keys go;
* the editing log keeps its lines but the actor is scrubbed to "Deleted user".
Everything runs in one transaction (the caller commits)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.bootstrap import lock_user_provisioning
from app.models.auth import OAuthIdentity, PasswordResetToken, RefreshToken
from app.models.chat_session import ChatSession
from app.models.evaluation import Evaluation
from app.models.idempotency_key import IdempotencyKey
from app.models.notification import Notification
from app.models.scorecard import Scorecard
from app.models.sharing import ScorecardActivity, ScorecardCollaborator, ScorecardInvitation
from app.models.user import ROLE_ADMIN, User
from app.slots import ACTIVE_JOB_STATUSES

logger = logging.getLogger(__name__)

DELETED_NAME = "Deleted user"


class AdminRuleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


async def active_admin_count(db: AsyncSession, *, excluding: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(User).where(
        User.role == ROLE_ADMIN, User.is_active.is_(True), User.deleted_at.is_(None)
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return int((await db.execute(stmt)).scalar_one())


async def guard_admin_change(db: AsyncSession, actor: User, target: User, *, losing_admin: bool, what: str) -> None:
    """Refuses acting on yourself and removing the last active admin. Takes the user-provisioning advisory lock so two
    admins demoting each other at the same moment cannot both pass the check."""
    await lock_user_provisioning(db)
    if target.id == actor.id:
        raise AdminRuleError("self_action", f"You cannot {what} your own account.")
    if losing_admin and target.role == ROLE_ADMIN and target.is_active and await active_admin_count(
        db, excluding=target.id
    ) == 0:
        raise AdminRuleError("last_admin", f"You cannot {what} the last active administrator.")


async def stop_user_work(user_id: uuid.UUID) -> None:
    """Cancel the user's active evaluation jobs and running chat turn so their slots free at once (own sessions)."""
    from app.api.v1.chat import cancel_background_turn
    from app.db import AsyncSessionLocal
    from app.pipeline.dispatcher import get_dispatcher

    async with AsyncSessionLocal() as db:
        eval_ids = (
            await db.execute(
                select(Evaluation.id).where(Evaluation.owner_id == user_id, Evaluation.status.in_(ACTIVE_JOB_STATUSES))
            )
        ).scalars().all()
        chat_ids = (
            await db.execute(
                select(ChatSession.id).where(
                    ChatSession.user_id == user_id, ChatSession.pending_turn_started_at.is_not(None)
                )
            )
        ).scalars().all()
    for eid in eval_ids:
        try:
            await get_dispatcher().cancel(eid)
        except Exception:  # noqa: BLE001 - finished meanwhile
            logger.info("could not cancel evaluation %s of a deactivated user", eid)
    for sid in chat_ids:
        try:
            await cancel_background_turn(sid)
        except Exception:  # noqa: BLE001
            logger.info("could not cancel the chat turn %s of a deactivated user", sid)


async def deactivate(db: AsyncSession, user: User) -> None:
    user.is_active = False
    await auth_service.revoke_all_sessions(db, user)  # refresh tokens revoked + access tokens invalid at once


async def anonymise_and_delete(db: AsyncSession, user: User) -> dict:
    from app.api.v1.scorecards import delete_scorecard_cascade

    summary = {"charts_deleted": 0, "charts_transferred": 0}
    owned = (await db.execute(select(Scorecard).where(Scorecard.owner_id == user.id))).scalars().all()
    for card in owned:
        successor = (
            await db.execute(
                select(ScorecardCollaborator)
                .where(ScorecardCollaborator.scorecard_id == card.id, ScorecardCollaborator.user_id != user.id)
                .order_by(ScorecardCollaborator.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if successor is None:
            await delete_scorecard_cascade(db, card)
            summary["charts_deleted"] += 1
        else:
            card.owner_id = successor.user_id
            await db.delete(successor)
            summary["charts_transferred"] += 1
            db.add(
                ScorecardActivity(
                    scorecard_id=card.id, actor_id=None, actor_name=DELETED_NAME, action="ownership_transferred",
                    entity_type="user", entity_id=successor.user_id,
                    summary="Ownership moved to a collaborator because the previous owner's account was deleted",
                )
            )
    await db.flush()

    # Chats are private: delete them (messages / turn events cascade; LangGraph checkpoints are removed best effort).
    chat_ids = (await db.execute(select(ChatSession.id).where(ChatSession.user_id == user.id))).scalars().all()
    await db.execute(delete(ChatSession).where(ChatSession.user_id == user.id))
    for table_where in (
        delete(ScorecardCollaborator).where(ScorecardCollaborator.user_id == user.id),
        delete(ScorecardInvitation).where(
            (ScorecardInvitation.invitee_id == user.id) | (ScorecardInvitation.inviter_id == user.id)
        ),
        delete(Notification).where(Notification.user_id == user.id),
        delete(RefreshToken).where(RefreshToken.user_id == user.id),
        delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id),
        delete(OAuthIdentity).where(OAuthIdentity.user_id == user.id),
        delete(IdempotencyKey).where(IdempotencyKey.user_id == user.id),
    ):
        await db.execute(table_where)

    # Scrub the (otherwise append-only) editing log: the trigger allows it only under this transaction-local flag.
    await db.execute(text("SET LOCAL qs.allow_activity_scrub = 'on'"))
    await db.execute(
        update(ScorecardActivity)
        .where(ScorecardActivity.actor_id == user.id)
        .values(actor_id=None, actor_name=DELETED_NAME)
    )
    await db.execute(text("SET LOCAL qs.allow_activity_scrub = 'off'"))

    # Versions the user created stay (FK RESTRICT); evaluations they ran stay with their chart.
    user.email = f"deleted-{user.id.hex}@deleted.invalid"
    user.name = DELETED_NAME
    user.username = None
    user.password_hash = None
    user.auth_provider_id = None
    user.is_active = False
    user.deleted_at = datetime.now(UTC)
    user.sessions_valid_after = datetime.now(UTC)
    user.failed_logins = 0
    user.locked_until = None
    summary["chat_ids"] = [str(c) for c in chat_ids]
    return summary


async def drop_checkpoints(chat_ids: list[str]) -> None:
    from app.ai.scorecard_builder import delete_session_checkpoints

    for cid in chat_ids:
        try:
            await delete_session_checkpoints(cid)
        except Exception:  # noqa: BLE001 - orphaned checkpoint rows are unreachable and harmless
            logger.warning("could not delete checkpoints of chat %s", cid, exc_info=True)

