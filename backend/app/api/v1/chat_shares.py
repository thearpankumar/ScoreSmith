"""Sharing a CHAT with another user (read-only), optionally together with its chart.

* "Chat only": the recipient sees the conversation and the KPI draft (the KPIs) but cannot talk to the assistant in it;
  to keep the result they save THEIR OWN copy of the chart (`POST /chat/shared/{id}/save`).
* "Chat and chart": additionally invites the recipient to the chart the chat produced, as a normal collaboration
  invitation (they accept it, edit it together with the others, and the chart's evaluations come with it).
* RE-SHARING (chains): a recipient may share the chat onward, still read-only (`POST /chat/sessions/{id}/shares` is open
  to the owner AND to every recipient); "chat and chart" from a recipient needs that they can invite to the chart
  themselves (they accepted it). The owner sees and may revoke every share; a recipient sees and revokes only the
  shares THEY made (403 `owner_only` for the others). The shared view says who passed it on to the viewer.
Chats stay private otherwise: nothing here lets a non-recipient (admins included) read a session."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import notifications as notif
from app.ai.draft_materialize import materialize_draft
from app.ai.draft_schema import ScorecardDraft
from app.ai.scorecard_builder import get_session_state
from app.api.v1.sharing import _find_active_user, check_lookup_budget, send_chart_invitation
from app.audit import audit
from app.authz import get_accessible_scorecard
from app.db import get_db
from app.deps import get_current_user
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.enums import AuditAction
from app.models.scorecard import Scorecard
from app.models.sharing import ChatShare, ScorecardCollaborator, ScorecardInvitation
from app.models.user import User
from app.ratelimit import rate_limit
from app.schemas.chat import ChatMessageRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat-shares"])

_share_limit = rate_limit("share", lambda s: s.rate_limit_share, per_user=True)


class ChatShareRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=320)
    # True: also invite the recipient to the chart this chat produced (needs a saved chart).
    with_chart: bool = False


class ChatShareRead(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    recipient_id: uuid.UUID
    recipient_name: str
    recipient_email: str
    with_chart: bool
    chart_invitation_status: str | None = None
    created_at: datetime
    shared_by_id: uuid.UUID | None = None
    shared_by_name: str | None = None


class SharedChatSummary(BaseModel):
    session_id: uuid.UUID
    title: str | None
    owner_name: str
    shared_by_name: str | None = None  # who passed it on to the viewer (the owner, or an earlier recipient)
    with_chart: bool
    shared_at: datetime
    last_activity_at: datetime
    saved_scorecard_id: uuid.UUID | None = None


class SharedChatRead(SharedChatSummary):
    messages: list[ChatMessageRead]
    draft: dict
    status: str
    can_save: bool
    # The chart behind the chat, only when the viewer can open it (they were invited with the chart and accepted).
    linked_scorecard_id: uuid.UUID | None = None


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": error, "message": message})


async def _share_read(db: AsyncSession, share: ChatShare, invitation_status: str | None = None) -> ChatShareRead:
    person = await db.get(User, share.recipient_id)
    sharer = await db.get(User, share.shared_by)
    return ChatShareRead(
        id=share.id, session_id=share.session_id, recipient_id=share.recipient_id, recipient_name=person.name,
        recipient_email=person.email, with_chart=share.with_chart, chart_invitation_status=invitation_status,
        created_at=share.created_at, shared_by_id=share.shared_by, shared_by_name=sharer.name if sharer else None,
    )


async def _chart_invitation_status(db: AsyncSession, session: ChatSession, share: ChatShare) -> str | None:
    """Status of the chart invitation that went with a "chat and chart" share, for the sender's status list:
    accepted (they collaborate now) | pending | declined | revoked | None (chat only / no chart)."""
    if not share.with_chart or session.target_scorecard_id is None:
        return None
    joined = (
        await db.execute(
            select(ScorecardCollaborator.id).where(
                ScorecardCollaborator.scorecard_id == session.target_scorecard_id,
                ScorecardCollaborator.user_id == share.recipient_id,
            )
        )
    ).first()
    if joined is not None:
        return "accepted"
    latest = (
        await db.execute(
            select(ScorecardInvitation.status)
            .where(
                ScorecardInvitation.scorecard_id == session.target_scorecard_id,
                ScorecardInvitation.invitee_id == share.recipient_id,
            )
            .order_by(ScorecardInvitation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return latest


async def _sharable_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> tuple[ChatSession, bool]:
    """The chat if the caller OWNS it or has received it (-> may pass it on); 404 for everybody else (admins too).
    Returns (session, is_owner)."""
    session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if session is not None and session.user_id == user.id:
        return session, True
    if session is not None:
        got = (
            await db.execute(
                select(ChatShare.id).where(ChatShare.session_id == session_id, ChatShare.recipient_id == user.id)
            )
        ).first()
        if got is not None:
            return session, False
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")


@router.post(
    "/sessions/{session_id}/shares",
    response_model=ChatShareRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_share_limit)],
)
async def share_chat(
    session_id: uuid.UUID,
    payload: ChatShareRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatShareRead:
    session, is_owner = await _sharable_session(db, user, session_id)  # the owner or any recipient (chains)
    scorecard: Scorecard | None = None
    if payload.with_chart:
        if session.target_scorecard_id is None:
            raise _err(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "no_chart_yet",
                "This chat has no saved chart yet. Save the chart first, or share the chat only.",
            )
        scorecard = await get_accessible_scorecard(db, user, session.target_scorecard_id)
    await check_lookup_budget(user)
    target = await _find_active_user(db, payload.identifier)
    if target is None:
        raise _err(status.HTTP_404_NOT_FOUND, "user_not_found", "No active user matches that username or email.")
    if target.id == user.id:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "self_invite", "That is you - you already have this chat.")
    if target.id == session.user_id:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "is_owner", "That person owns this chat.")

    share = (
        await db.execute(
            select(ChatShare).where(ChatShare.session_id == session.id, ChatShare.recipient_id == target.id)
        )
    ).scalar_one_or_none()
    created = share is None
    if share is None:
        share = ChatShare(session_id=session.id, recipient_id=target.id, shared_by=user.id, with_chart=False)
        db.add(share)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise _err(status.HTTP_409_CONFLICT, "already_shared", "Already shared with that person.") from None
    upgrading = payload.with_chart and not share.with_chart
    if payload.with_chart:
        share.with_chart = True
    title = session.title or "a chat"
    if created and not is_owner:  # tell the chat owner that it travelled on
        await notif.add_notification(
            db, session.user_id, notif.CHAT_SHARED, f"{user.name} shared your chat with {target.name}",
            body=f"Chat: {title}", data={"session_id": str(session.id), "reshared_by": str(user.id)},
            link=f"/chat/{session.id}", dedupe_key=f"chatreshare:{share.id}",
        )
    if created or upgrading:
        await notif.add_notification(
            db, target.id, notif.CHAT_SHARED,
            f"{user.name} shared a chat with you" + (" and its chart" if payload.with_chart else ""),
            body=f"Chat: {title}", data={"session_id": str(session.id), "with_chart": payload.with_chart},
            link=f"/chat/shared/{session.id}", dedupe_key=f"chatshare:{share.id}:{int(payload.with_chart)}",
        )
    audit(db, request, actor_id=user.id, entity_type="chat_share", entity_id=share.id, action=AuditAction.CREATE,
          event="chat_shared", diff={"with_chart": payload.with_chart})
    await db.commit()
    invitation_status = None
    if scorecard is not None:
        try:
            inv, _pending = await send_chart_invitation(db, request, user, scorecard, target)
            invitation_status = inv.status
        except HTTPException as exc:  # e.g. the person already collaborates on the chart: the chat share still stands
            invitation_status = "accepted" if getattr(exc, "detail", {}).get("code") == "already_collaborator" else None
            if invitation_status is None:
                raise
    return await _share_read(db, share, invitation_status)


@router.get("/sessions/{session_id}/shares", response_model=list[ChatShareRead])
async def list_chat_shares(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ChatShareRead]:
    session, is_owner = await _sharable_session(db, user, session_id)
    query = select(ChatShare).where(ChatShare.session_id == session_id)
    if not is_owner:  # a recipient only sees the shares THEY made
        query = query.where(ChatShare.shared_by == user.id)
    rows = (await db.execute(query.order_by(ChatShare.created_at))).scalars().all()
    return [await _share_read(db, s, await _chart_invitation_status(db, session, s)) for s in rows]


@router.delete(
    "/sessions/{session_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def revoke_chat_share(
    session_id: uuid.UUID,
    share_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    _session, is_owner = await _sharable_session(db, user, session_id)
    share = (
        await db.execute(select(ChatShare).where(ChatShare.id == share_id, ChatShare.session_id == session_id))
    ).scalar_one_or_none()
    if share is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Share not found.")
    if not is_owner and share.shared_by != user.id:
        raise _err(status.HTTP_403_FORBIDDEN, "owner_only", "Only the chat's owner can remove this share.")
    await db.delete(share)
    audit(db, request, actor_id=user.id, entity_type="chat_share", entity_id=share_id, action=AuditAction.DELETE,
          event="chat_share_revoked")
    await db.commit()


# --- recipient side -------------------------------------------------------------------------------------------


async def _shared_with(db: AsyncSession, user: User, session_id: uuid.UUID) -> tuple[ChatShare, ChatSession]:
    row = (
        await db.execute(
            select(ChatShare, ChatSession)
            .join(ChatSession, ChatSession.id == ChatShare.session_id)
            .where(ChatShare.session_id == session_id, ChatShare.recipient_id == user.id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
    return row[0], row[1]


@router.get("/shared", response_model=list[SharedChatSummary])
async def list_shared_chats(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[SharedChatSummary]:
    rows = (
        await db.execute(
            select(ChatShare, ChatSession, User.name)
            .join(ChatSession, ChatSession.id == ChatShare.session_id)
            .join(User, User.id == ChatSession.user_id)
            .where(ChatShare.recipient_id == user.id)
            .order_by(ChatShare.created_at.desc())
            .limit(200)
        )
    ).all()
    sharer_ids = {sh.shared_by for sh, _s, _o in rows}
    names = (
        {u.id: u.name for u in (await db.execute(select(User).where(User.id.in_(sharer_ids)))).scalars()}
        if sharer_ids
        else {}
    )
    return [
        SharedChatSummary(
            session_id=s.id, title=s.title, owner_name=owner, with_chart=sh.with_chart, shared_at=sh.created_at,
            last_activity_at=s.last_activity_at, saved_scorecard_id=sh.saved_scorecard_id,
            shared_by_name=names.get(sh.shared_by),
        )
        for sh, s, owner in rows
    ]


@router.get("/shared/{session_id}", response_model=SharedChatRead)
async def read_shared_chat(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SharedChatRead:
    share, session = await _shared_with(db, user, session_id)
    owner = await db.get(User, session.user_id)
    sharer = await db.get(User, share.shared_by)
    messages = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
        )
    ).scalars().all()
    turn = await get_session_state(str(session_id))
    draft = dict(turn.draft) if turn is not None and turn.draft else {}
    linked = None
    if session.target_scorecard_id is not None:
        has_access = (
            await db.execute(
                select(ScorecardCollaborator.id)
                .join(Scorecard, Scorecard.id == ScorecardCollaborator.scorecard_id)
                .where(
                    ScorecardCollaborator.scorecard_id == session.target_scorecard_id,
                    ScorecardCollaborator.user_id == user.id,
                    Scorecard.deleted_at.is_(None),
                )
            )
        ).first()
        linked = session.target_scorecard_id if has_access else None
    complete = False
    try:
        complete = bool(draft) and ScorecardDraft.model_validate(draft).is_complete()
    except Exception:  # noqa: BLE001 - a half-built draft just cannot be saved yet
        complete = False
    return SharedChatRead(
        session_id=session.id, title=session.title, owner_name=owner.name if owner else "Someone",
        with_chart=share.with_chart, shared_at=share.created_at, last_activity_at=session.last_activity_at,
        saved_scorecard_id=share.saved_scorecard_id,
        shared_by_name=sharer.name if sharer else None,
        messages=[ChatMessageRead.model_validate(m) for m in messages], draft=draft,
        status=turn.status if turn is not None else "gathering", can_save=complete, linked_scorecard_id=linked,
    )


@router.post("/shared/{session_id}/save", dependencies=[Depends(_share_limit)])
async def save_shared_chat_as_my_chart(
    session_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """The recipient keeps the chat's KPIs by saving THEIR OWN chart from the shared draft (original untouched)."""
    share, _session = await _shared_with(db, user, session_id)
    if share.saved_scorecard_id is not None and await db.get(Scorecard, share.saved_scorecard_id) is not None:
        return {"scorecard_id": str(share.saved_scorecard_id), "already_saved": True}
    turn = await get_session_state(str(session_id))
    if turn is None or not turn.draft:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_draft", "This chat has no KPIs to save yet.")
    try:
        draft = ScorecardDraft.model_validate(turn.draft)
        scorecard, _version = await materialize_draft(db, draft, owner_id=user.id)
    except ValueError as exc:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "draft_incomplete", str(exc)) from exc
    share.saved_scorecard_id = scorecard.id
    audit(db, request, actor_id=user.id, entity_type="scorecard", entity_id=scorecard.id, action=AuditAction.CREATE,
          event="saved_from_shared_chat", diff={"session_id": str(session_id)})
    await db.commit()
    return {"scorecard_id": str(scorecard.id), "already_saved": False}
