from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class UserBrief(BaseModel):
    id: uuid.UUID
    name: str
    email: str | None = None
    username: str | None = None


class InviteRequest(BaseModel):
    # A username or an email address; matched case-insensitively against ACTIVE accounts.
    identifier: str = Field(min_length=1, max_length=320)
    # Also share the caller's own chat that produced this chart (read-only, together with the chart).
    include_chat: bool = False


class InvitationRead(BaseModel):
    id: uuid.UUID
    scorecard_id: uuid.UUID
    scorecard_name: str
    status: str
    role: str
    inviter: UserBrief
    invitee: UserBrief
    created_at: datetime
    responded_at: datetime | None = None
    already_pending: bool = False
    chat_shared: bool = False  # the invite also carried the sender's source chat (include_chat)


class CollaboratorRead(BaseModel):
    user: UserBrief
    role: str
    joined_at: datetime


class SharingRead(BaseModel):
    scorecard_id: uuid.UUID
    my_role: str  # owner | editor
    owner: UserBrief
    collaborators: list[CollaboratorRead]
    # Everything the owner has sent (any status), newest first. Empty for collaborators.
    invitations: list[InvitationRead]
    # The caller's own chat that produced this chart, if any (the share dialog offers "chart and its chat").
    source_chat_id: uuid.UUID | None = None


class ActivityRead(BaseModel):
    id: int
    scorecard_id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_name: str
    action: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    summary: str
    detail: dict | None
    created_at: datetime


class ActivityPage(BaseModel):
    items: list[ActivityRead]
    next_before: int | None = None


class PreviewKpi(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    level: int
    name: str
    weight: float | None
    display_order: int
    included_in_scoring: bool


class InvitationPreview(BaseModel):
    invitation: InvitationRead
    owner_name: str
    name: str
    domain: str | None
    purpose_statement: str | None
    target_score: float | None
    version_number: int | None
    scoring_formula: str | None
    kpis: list[PreviewKpi]


class NotificationRead(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None
    data: dict | None
    link: str | None
    created_at: datetime
    read_at: datetime | None
    # Only for `invite_received`: the live status of the invitation, so the panel shows Accept / Decline only while
    # it is still pending.
    invitation_status: str | None = None


class NotificationPage(BaseModel):
    items: list[NotificationRead]
    next_cursor: str | None = None
    unread: int = 0


class UnreadCount(BaseModel):
    unread: int
