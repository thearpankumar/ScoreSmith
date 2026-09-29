from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPKMixin
from app.models.enums import ChatSessionStatus

if TYPE_CHECKING:
    from app.models.chat_message import ChatMessage

chat_session_status_enum = ENUM(
    ChatSessionStatus, name="chat_session_status", create_type=False,
    values_callable=lambda e: [m.value for m in e],
)

# How long `pending_turn_started_at` (below) is trusted before being treated as stale —
# see that column's docstring. Generous margin over the documented ~20-90s multi-agent
# research fan-out ceiling, so a genuinely slow (but alive) turn is never mistaken for a
# crashed one, while a process that died mid-turn (skipping the finally-block clear in
# app/api/v1/chat.py) doesn't wedge the UI in "still working" forever.
STALE_TURN_TIMEOUT_SECONDS = 300


class ChatSession(UUIDPKMixin, Base):
    __tablename__ = "chat_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[ChatSessionStatus] = mapped_column(
        chat_session_status_enum, nullable=False, default=ChatSessionStatus.ACTIVE
    )
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_scorecard_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecards.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Persistent "a turn is in flight" marker (see app/api/v1/chat.py's
    # _mark_turn_in_progress/_clear_turn_in_progress): the HTTP request for a chat turn
    # blocks synchronously for the whole LangGraph run today (no background job queue), so
    # a page refresh mid-turn has nothing to "reconnect" to except this DB row. Set to the
    # turn's start time right before the (possibly 20-90+s, multi-agent-research) graph
    # call begins, cleared to NULL when it ends (success OR failure) — see
    # STALE_TURN_TIMEOUT_SECONDS in chat.py for the staleness fallback if a crash ever
    # skips the clear.
    pending_turn_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    messages: Mapped[list[ChatMessage]] = relationship(
        "ChatMessage", back_populates="session", cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )

    @property
    def turn_in_progress(self) -> bool:
        """True when a turn is currently running server-side for this session AND that
        marker isn't stale (see STALE_TURN_TIMEOUT_SECONDS). Read directly by
        `ChatSessionRead`/`ChatTurnRead` (both `from_attributes=True`) — see
        app/api/v1/chat.py for where `pending_turn_started_at` is set/cleared."""
        if self.pending_turn_started_at is None:
            return False
        started = self.pending_turn_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        age = datetime.now(UTC) - started
        return age < timedelta(seconds=STALE_TURN_TIMEOUT_SECONDS)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatSession id={self.id} status={self.status}>"
