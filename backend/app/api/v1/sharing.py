"""Chart sharing by invitation: send / revoke invitations, list collaborators, remove / leave, the editing log, and the
invitee's side (list, read-only preview, accept, decline). Authorization lives in app/authz.py; see
docs/plan-sharing-rbac.md for the permission matrix.

Enumeration trade-off (deliberate, requested): the sender learns whether a username / email belongs to an active
account. Mitigations: authenticated senders only, every attempt counts against the SENDER's rate limit
(`RATE_LIMIT_SHARE_LOOKUP`, shared across replicas through Redis), deactivated / deleted / system accounts answer
exactly like unknown ones, and a user can never invite themselves."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import notifications as notif
from app.activity import record_activity
from app.audit import audit
from app.auth.handles import find_user_by_handle
from app.authz import get_accessible_scorecard, get_owned_scorecard
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models.chat_session import ChatSession
from app.models.enums import AuditAction
from app.models.evaluation import Evaluation
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.sharing import (
    INVITE_ACCEPTED,
    INVITE_DECLINED,
    INVITE_PENDING,
    INVITE_REVOKED,
    ROLE_EDITOR,
    ChatShare,
    ScorecardActivity,
    ScorecardCollaborator,
    ScorecardInvitation,
)
from app.models.user import User
from app.pipeline.dispatcher import get_dispatcher
from app.ratelimit import rate_limit, within_limit
from app.schemas.sharing import (
    ActivityPage,
    ActivityRead,
    CollaboratorRead,
    InvitationPreview,
    InvitationRead,
    InviteRequest,
    PreviewKpi,
    SharingRead,
    UserBrief,
)
from app.slots import ACTIVE_JOB_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sharing"])

_share_limit = rate_limit("share", lambda s: s.rate_limit_share, per_user=True)


def _brief(u: User, *, with_email: bool = True) -> UserBrief:
    return UserBrief(id=u.id, name=u.name, email=u.email if with_email else None, username=u.username)


def _err(code: int, error: str, message: str, **extra: object) -> HTTPException:
    return HTTPException(code, detail={"code": error, "message": message, **extra})


async def _invitation_read(
    db: AsyncSession, inv: ScorecardInvitation, *, already_pending: bool = False
) -> InvitationRead:
    scorecard = await db.get(Scorecard, inv.scorecard_id)
    inviter = await db.get(User, inv.inviter_id)
    invitee = await db.get(User, inv.invitee_id)
    return InvitationRead(
        id=inv.id, scorecard_id=inv.scorecard_id, scorecard_name=scorecard.name if scorecard else "",
        status=inv.status, role=inv.role, inviter=_brief(inviter), invitee=_brief(invitee),
        created_at=inv.created_at, responded_at=inv.responded_at, already_pending=already_pending,
    )


async def _find_active_user(db: AsyncSession, identifier: str) -> User | None:
    return await find_user_by_handle(db, identifier, active_only=True)


async def cancel_user_jobs_on_scorecard(user_id: uuid.UUID, scorecard_id: uuid.UUID) -> int:
    """A collaborator who is removed (or leaves) must not keep spending on the chart: their ACTIVE evaluations on it
    are cancelled (rows already finished stay with the chart). Cancels go through the dispatcher so the worker that
    drives them stops too; best effort per row."""
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ids = (
            await db.execute(
                select(Evaluation.id)
                .join(ScorecardVersion, ScorecardVersion.id == Evaluation.scorecard_version_id)
                .where(
                    ScorecardVersion.scorecard_id == scorecard_id,
                    Evaluation.owner_id == user_id,
                    Evaluation.status.in_(ACTIVE_JOB_STATUSES),
                )
            )
        ).scalars().all()
    dispatcher = get_dispatcher()
    cancelled = 0
    for eid in ids:
        try:
            await dispatcher.cancel(eid)
            cancelled += 1
        except Exception:  # noqa: BLE001 - already finished meanwhile
            logger.info("could not cancel evaluation %s while removing its runner", eid)
    return cancelled


# --- owner side ---------------------------------------------------------------------------------------


@router.post(
    "/scorecards/{scorecard_id}/invitations",
    response_model=InvitationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_share_limit)],
)
async def invite_collaborator(
    scorecard_id: uuid.UUID,
    payload: InviteRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InvitationRead:
    # The owner AND any accepted collaborator may invite further people (re-sharing); everyone else gets 404.
    scorecard = await get_accessible_scorecard(db, user, scorecard_id)
    await check_lookup_budget(user)
    target = await _find_active_user(db, payload.identifier)
    if target is None:
        raise _err(status.HTTP_404_NOT_FOUND, "user_not_found", "No active user matches that username or email.")
    if payload.include_chat and await _source_chat(db, user, scorecard.id) is None:  # refuse BEFORE inviting
        raise _err(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "no_source_chat",
            "You have no chat behind this chart to share. Share the chart only.",
        )
    inv, already_pending = await send_chart_invitation(db, request, user, scorecard, target)
    if already_pending:
        response.status_code = status.HTTP_200_OK
    chat_shared = False
    if payload.include_chat:
        chat_shared = await _share_source_chat(db, request, user, scorecard, target)
    out = await _invitation_read(db, inv, already_pending=already_pending)
    out.chat_shared = chat_shared
    return out


async def _source_chat(db: AsyncSession, user: User, scorecard_id: uuid.UUID) -> ChatSession | None:
    return (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user.id, ChatSession.target_scorecard_id == scorecard_id)
            .order_by(ChatSession.last_activity_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _share_source_chat(
    db: AsyncSession, request: Request, user: User, scorecard: Scorecard, target: User
) -> bool:
    """"Chart and its chat": read-only share of the caller's own chat that built / refined this chart."""
    chat = await _source_chat(db, user, scorecard.id)
    if chat is None:
        raise _err(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "no_source_chat",
            "You have no chat behind this chart to share. Share the chart only.",
        )
    share = (
        await db.execute(select(ChatShare).where(ChatShare.session_id == chat.id, ChatShare.recipient_id == target.id))
    ).scalar_one_or_none()
    if share is None:
        share = ChatShare(session_id=chat.id, recipient_id=target.id, shared_by=user.id, with_chart=True)
        db.add(share)
        await db.flush()
    elif share.with_chart:
        return True
    else:
        share.with_chart = True
    await notif.add_notification(
        db, target.id, notif.CHAT_SHARED, f"{user.name} shared a chat with you and its chart",
        body=f"Chat: {chat.title or 'a chat'}", data={"session_id": str(chat.id), "with_chart": True},
        link=f"/chat/shared/{chat.id}", dedupe_key=f"chatshare:{share.id}:1",
    )
    audit(db, request, actor_id=user.id, entity_type="chat_share", entity_id=share.id, action=AuditAction.CREATE,
          event="chat_shared", diff={"with_chart": True, "via": "chart_share"})
    await db.commit()
    return True


async def check_lookup_budget(user: User) -> None:
    """Every invite / share attempt counts against the SENDER (bounds username / email probing)."""
    if not await within_limit("share-lookup", get_settings().rate_limit_share_lookup, str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "lookup_rate_limited", "message": "Too many invitations in a short time. Try again later."},
            headers={"Retry-After": "600"},
        )


async def send_chart_invitation(
    db: AsyncSession, request: Request | None, user: User, scorecard: Scorecard, target: User
) -> tuple[ScorecardInvitation, bool]:
    """Creates (or finds the pending) invitation of `target` to `scorecard` and commits. Returns (invitation,
    already_pending). Shared by the chart share dialog and "share this chat together with its chart"."""
    if target.id == user.id:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "self_invite", "You already have access to this chart.")
    already = (
        await db.execute(
            select(ScorecardCollaborator.id).where(
                ScorecardCollaborator.scorecard_id == scorecard.id, ScorecardCollaborator.user_id == target.id
            )
        )
    ).first()
    if already is not None or target.id == scorecard.owner_id:
        raise _err(
            status.HTTP_409_CONFLICT, "already_collaborator", f"{target.name} already collaborates on this chart."
        )

    async def pending_of() -> ScorecardInvitation | None:
        return (
            await db.execute(
                select(ScorecardInvitation).where(
                    ScorecardInvitation.scorecard_id == scorecard.id,
                    ScorecardInvitation.invitee_id == target.id,
                    ScorecardInvitation.status == INVITE_PENDING,
                )
            )
        ).scalar_one_or_none()

    pending = await pending_of()
    if pending is not None:
        return pending, True
    inv = ScorecardInvitation(
        scorecard_id=scorecard.id, inviter_id=user.id, invitee_id=target.id, role=ROLE_EDITOR, status=INVITE_PENDING
    )
    db.add(inv)
    try:
        await db.flush()
    except IntegrityError:  # a concurrent identical invite won the race: answer like the duplicate case
        await db.rollback()
        return await pending_of(), True  # type: ignore[return-value]
    await notif.add_notification(
        db, target.id, notif.INVITE_RECEIVED, f"{user.name} invited you to collaborate",
        body=f"Chart: {scorecard.name}", data={"invitation_id": str(inv.id), "scorecard_id": str(scorecard.id)},
        link=f"/charts?invite={inv.id}", dedupe_key=f"invite:{inv.id}",
    )
    record_activity(
        db, scorecard.id, user, "collaborator_invited", f"Invited {target.name} to collaborate",
        entity_type="user", entity_id=target.id, detail={"invitee": target.name},
    )
    audit(db, request, actor_id=user.id, entity_type="scorecard_invitation", entity_id=inv.id,
          action=AuditAction.CREATE, event="invited")
    await db.commit()
    await db.refresh(inv)
    return inv, False


@router.get("/scorecards/{scorecard_id}/sharing", response_model=SharingRead)
async def get_sharing(
    scorecard_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SharingRead:
    scorecard = await get_accessible_scorecard(db, user, scorecard_id)
    owner = await db.get(User, scorecard.owner_id)
    rows = (
        await db.execute(
            select(ScorecardCollaborator, User)
            .join(User, User.id == ScorecardCollaborator.user_id)
            .where(ScorecardCollaborator.scorecard_id == scorecard.id)
            .order_by(ScorecardCollaborator.created_at)
        )
    ).all()
    is_owner = scorecard.owner_id == user.id
    stmt = select(ScorecardInvitation).where(ScorecardInvitation.scorecard_id == scorecard.id)
    if not is_owner:  # an editor sees (and can revoke) only the invitations they sent themselves
        stmt = stmt.where(ScorecardInvitation.inviter_id == user.id)
    invs = (await db.execute(stmt.order_by(ScorecardInvitation.created_at.desc()).limit(100))).scalars().all()
    invitations = [await _invitation_read(db, i) for i in invs]
    source_chat = await _source_chat(db, user, scorecard.id)
    return SharingRead(
        source_chat_id=source_chat.id if source_chat is not None else None,
        scorecard_id=scorecard.id,
        my_role="owner" if is_owner else "editor",
        owner=_brief(owner),
        collaborators=[CollaboratorRead(user=_brief(u), role=c.role, joined_at=c.created_at) for c, u in rows],
        invitations=invitations,
    )


@router.delete(
    "/scorecards/{scorecard_id}/invitations/{invitation_id}",
    response_model=InvitationRead,
    dependencies=[Depends(_share_limit)],
)
async def revoke_invitation(
    scorecard_id: uuid.UUID,
    invitation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InvitationRead:
    scorecard = await get_accessible_scorecard(db, user, scorecard_id)
    inv = (
        await db.execute(
            select(ScorecardInvitation)
            .where(ScorecardInvitation.id == invitation_id, ScorecardInvitation.scorecard_id == scorecard.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if inv is None or (scorecard.owner_id != user.id and inv.inviter_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    if inv.status != INVITE_PENDING:
        raise _err(status.HTTP_409_CONFLICT, "invitation_not_pending", f"This invitation is already {inv.status}.")
    inv.status, inv.responded_at = INVITE_REVOKED, datetime.now(UTC)
    invitee = await db.get(User, inv.invitee_id)
    await notif.add_notification(
        db, inv.invitee_id, notif.INVITE_REVOKED, f"{user.name} withdrew the invitation",
        body=f"Chart: {scorecard.name}", data={"invitation_id": str(inv.id)}, dedupe_key=f"invite:{inv.id}:revoked",
    )
    record_activity(
        db, scorecard.id, user, "invitation_revoked",
        f"Withdrew the invitation to {invitee.name if invitee else 'a user'}",
        entity_type="user", entity_id=inv.invitee_id,
    )
    audit(db, request, actor_id=user.id, entity_type="scorecard_invitation", entity_id=inv.id,
          action=AuditAction.UPDATE, event="invitation_revoked")
    await db.commit()
    return await _invitation_read(db, inv)


@router.delete(
    "/scorecards/{scorecard_id}/collaborators/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(_share_limit)],
)
async def remove_collaborator(
    scorecard_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    scorecard = await get_owned_scorecard(db, user, scorecard_id)
    removed = (
        await db.execute(
            select(ScorecardCollaborator)
            .where(ScorecardCollaborator.scorecard_id == scorecard.id, ScorecardCollaborator.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if removed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Collaborator not found.")
    person = await db.get(User, user_id)
    await db.delete(removed)
    await db.execute(
        update(ScorecardInvitation)
        .where(
            ScorecardInvitation.scorecard_id == scorecard.id,
            ScorecardInvitation.invitee_id == user_id,
            ScorecardInvitation.status == INVITE_ACCEPTED,
        )
        .values(status=INVITE_REVOKED, responded_at=datetime.now(UTC))
    )
    record_activity(
        db, scorecard.id, user, "collaborator_removed", f"Removed {person.name if person else 'a collaborator'}",
        entity_type="user", entity_id=user_id,
    )
    await notif.add_notification(
        db, user_id, notif.COLLABORATOR_REMOVED, f"You were removed from “{scorecard.name}”",
        body=f"{user.name} removed you from this chart.", data={"scorecard_id": str(scorecard.id)},
        dedupe_key=f"removed:{scorecard.id}:{user_id}:{int(datetime.now(UTC).timestamp())}",
    )
    audit(db, request, actor_id=user.id, entity_type="scorecard", entity_id=scorecard.id,
          action=AuditAction.UPDATE, event="collaborator_removed", diff={"user_id": str(user_id)})
    await db.commit()
    await cancel_user_jobs_on_scorecard(user_id, scorecard.id)


@router.post(
    "/scorecards/{scorecard_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(_share_limit)],
)
async def leave_scorecard(
    scorecard_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    scorecard = await get_accessible_scorecard(db, user, scorecard_id)
    await leave_chart(db, request, user, scorecard)


async def leave_chart(db: AsyncSession, request: Request | None, user: User, scorecard: Scorecard) -> None:
    """The collaborator `user` stops collaborating on `scorecard` (commits). Shared by `POST .../leave` and by
    `DELETE /scorecards/{id}` when the caller is not the owner ("delete" of a shared chart only removes you)."""
    if scorecard.owner_id == user.id:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "owner_cannot_leave", "Owners cannot leave their own chart.")
    await db.execute(
        ScorecardCollaborator.__table__.delete().where(
            ScorecardCollaborator.scorecard_id == scorecard.id, ScorecardCollaborator.user_id == user.id
        )
    )
    record_activity(db, scorecard.id, user, "collaborator_left", f"{user.name} left the chart",
                    entity_type="user", entity_id=user.id)
    await notif.add_notification(
        db, scorecard.owner_id, notif.COLLABORATOR_LEFT, f"{user.name} left “{scorecard.name}”",
        data={"scorecard_id": str(scorecard.id)},
        dedupe_key=f"left:{scorecard.id}:{user.id}:{int(datetime.now(UTC).timestamp())}",
    )
    audit(db, request, actor_id=user.id, entity_type="scorecard", entity_id=scorecard.id,
          action=AuditAction.UPDATE, event="collaborator_left")
    await db.commit()
    await cancel_user_jobs_on_scorecard(user.id, scorecard.id)


@router.get("/scorecards/{scorecard_id}/activity", response_model=ActivityPage)
async def list_activity(
    scorecard_id: uuid.UUID,
    before: int | None = Query(default=None, ge=1, description="Return entries older than this id."),
    limit: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ActivityPage:
    await get_accessible_scorecard(db, user, scorecard_id)
    stmt = select(ScorecardActivity).where(ScorecardActivity.scorecard_id == scorecard_id)
    if before is not None:
        stmt = stmt.where(ScorecardActivity.id < before)
    rows = (await db.execute(stmt.order_by(ScorecardActivity.id.desc()).limit(limit + 1))).scalars().all()
    page = rows[:limit]
    return ActivityPage(
        items=[
            ActivityRead(
                id=r.id, scorecard_id=r.scorecard_id, actor_id=r.actor_id, actor_name=r.actor_name, action=r.action,
                entity_type=r.entity_type, entity_id=r.entity_id, summary=r.summary, detail=r.detail,
                created_at=r.created_at,
            )
            for r in page
        ],
        next_before=page[-1].id if len(rows) > limit else None,
    )


# --- invitee side -------------------------------------------------------------------------------------


def _require_not_trashed(scorecard: Scorecard | None) -> None:
    if scorecard is not None and scorecard.deleted_at is not None:
        raise _err(
            status.HTTP_409_CONFLICT, "chart_in_trash",
            "This chart is currently in its owner's trash, so the invitation cannot be opened or accepted right now.",
        )


async def _own_invitation(db: AsyncSession, user: User, invitation_id: uuid.UUID, *, lock: bool = False):
    stmt = select(ScorecardInvitation).where(
        ScorecardInvitation.id == invitation_id, ScorecardInvitation.invitee_id == user.id
    )
    inv = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    return inv


@router.get("/invitations", response_model=list[InvitationRead])
async def list_my_invitations(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[InvitationRead]:
    invs = (
        await db.execute(
            select(ScorecardInvitation)
            .join(Scorecard, Scorecard.id == ScorecardInvitation.scorecard_id)
            .where(
                ScorecardInvitation.invitee_id == user.id,
                ScorecardInvitation.status == INVITE_PENDING,
                Scorecard.deleted_at.is_(None),  # an invitation to a chart in the trash is not offered
            )
            .order_by(ScorecardInvitation.created_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return [await _invitation_read(db, i) for i in invs]


@router.get("/invitations/{invitation_id}/preview", response_model=InvitationPreview)
async def preview_invitation(
    invitation_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> InvitationPreview:
    """Read-only look at the shared chart BEFORE accepting. Only the pending invitee can open it."""
    inv = await _own_invitation(db, user, invitation_id)
    if inv.status != INVITE_PENDING:
        raise _err(status.HTTP_409_CONFLICT, "invitation_not_pending", f"This invitation is {inv.status}.")
    scorecard = await db.get(Scorecard, inv.scorecard_id)
    _require_not_trashed(scorecard)
    owner = await db.get(User, scorecard.owner_id)
    version = await db.get(ScorecardVersion, scorecard.current_version_id) if scorecard.current_version_id else None
    nodes = []
    if version is not None:
        nodes = (
            await db.execute(
                select(KpiNode)
                .where(KpiNode.scorecard_version_id == version.id)
                .order_by(KpiNode.level, KpiNode.display_order)
            )
        ).scalars().all()
    return InvitationPreview(
        invitation=await _invitation_read(db, inv),
        owner_name=owner.name,
        name=scorecard.name,
        domain=scorecard.domain,
        purpose_statement=scorecard.purpose_statement,
        target_score=float(scorecard.target_score) if scorecard.target_score is not None else None,
        version_number=version.version_number if version else None,
        scoring_formula=version.scoring_formula if version else None,
        kpis=[
            PreviewKpi(
                id=n.id, parent_id=n.parent_id, level=n.level, name=n.name,
                weight=float(n.weight) if n.weight is not None else None, display_order=n.display_order,
                included_in_scoring=bool(n.included_in_scoring),
            )
            for n in nodes
        ],
    )


async def _respond(
    db: AsyncSession, request: Request, user: User, invitation_id: uuid.UUID, *, accept: bool
) -> InvitationRead:
    inv = await _own_invitation(db, user, invitation_id, lock=True)
    if inv.status != INVITE_PENDING:
        raise _err(status.HTTP_409_CONFLICT, "invitation_not_pending", f"This invitation is already {inv.status}.")
    scorecard = await db.get(Scorecard, inv.scorecard_id)
    if accept:
        _require_not_trashed(scorecard)
    inv.status = INVITE_ACCEPTED if accept else INVITE_DECLINED
    inv.responded_at = datetime.now(UTC)
    if accept:
        await db.execute(
            pg_insert(ScorecardCollaborator)
            .values(id=uuid.uuid4(), scorecard_id=inv.scorecard_id, user_id=user.id, role=inv.role,
                    invited_by=inv.inviter_id)
            .on_conflict_do_nothing()
        )
        inviter = await db.get(User, inv.inviter_id)
        via = f" (invited by {inviter.name})" if inviter is not None else ""
        record_activity(
            db, inv.scorecard_id, user, "collaborator_joined", f"{user.name} joined the chart{via}",
            entity_type="user", entity_id=user.id,
            detail={"invited_by": str(inv.inviter_id), "invited_by_name": inviter.name if inviter else None},
        )
    await notif.add_notification(
        db, inv.inviter_id, notif.INVITE_ACCEPTED if accept else notif.INVITE_DECLINED,
        f"{user.name} {'accepted' if accept else 'declined'} your invitation",
        body=f"Chart: {scorecard.name}" if scorecard else None,
        data={"invitation_id": str(inv.id), "scorecard_id": str(inv.scorecard_id)},
        link=f"/charts/{inv.scorecard_id}" if accept else None,
        dedupe_key=f"invite:{inv.id}:{'accepted' if accept else 'declined'}",
    )
    audit(db, request, actor_id=user.id, entity_type="scorecard_invitation", entity_id=inv.id,
          action=AuditAction.UPDATE, event="invitation_accepted" if accept else "invitation_declined")
    await db.commit()
    return await _invitation_read(db, inv)


@router.post("/invitations/{invitation_id}/accept", response_model=InvitationRead, dependencies=[Depends(_share_limit)])
async def accept_invitation(
    invitation_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InvitationRead:
    return await _respond(db, request, user, invitation_id, accept=True)


@router.post(
    "/invitations/{invitation_id}/decline", response_model=InvitationRead, dependencies=[Depends(_share_limit)]
)
async def decline_invitation(
    invitation_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InvitationRead:
    return await _respond(db, request, user, invitation_id, accept=False)

