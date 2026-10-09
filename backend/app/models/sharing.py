"""Chart sharing (migration 0013_sharing_rbac): collaborators, invitations and the append-only activity log."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin

ROLE_EDITOR = "editor"
INVITE_PENDING = "pending"
INVITE_ACCEPTED = "accepted"
INVITE_DECLINED = "declined"
INVITE_REVOKED = "revoked"


class ScorecardCollaborator(UUIDPKMixin, Base):
    __tablename__ = "scorecard_collaborators"
    __table_args__ = (UniqueConstraint("scorecard_id", "user_id", name="uq_scorecard_collaborators_sc_user"),)

    scorecard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_EDITOR)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScorecardInvitation(UUIDPKMixin, Base):
    __tablename__ = "scorecard_invitations"
    __table_args__ = (
        Index(
            "uq_scorecard_invitations_pending", "scorecard_id", "invitee_id", unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_scorecard_invitations_invitee_status", "invitee_id", "status"),
    )

    scorecard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inviter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    invitee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_EDITOR)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=INVITE_PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScorecardActivity(Base):
    """Append-only editing log (a BEFORE UPDATE trigger rejects updates). Rows go away only with their scorecard."""

    __tablename__ = "scorecard_activity"
    __table_args__ = (Index("ix_scorecard_activity_sc_id", "scorecard_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    scorecard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(200), nullable=False, default="Someone")
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatShare(UUIDPKMixin, Base):
    """A chat shared READ-ONLY with another user (migration 0014). The recipient sees the conversation and the KPI
    draft and can save their own copy of the chart (`saved_scorecard_id`); with `with_chart` the linked chart is
    ALSO shared as a normal collaboration invitation (evaluations then come with the chart)."""

    __tablename__ = "chat_shares"
    __table_args__ = (UniqueConstraint("session_id", "recipient_id", name="uq_chat_shares_session_recipient"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shared_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    with_chart: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    saved_scorecard_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecards.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
