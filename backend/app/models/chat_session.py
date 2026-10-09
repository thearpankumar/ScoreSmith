from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.models.base import Base, UUIDPKMixin
from app.models.enums import ChatSessionStatus

if TYPE_CHECKING:
    from app.models.chat_message import ChatMessage

chat_session_status_enum = ENUM(
    ChatSessionStatus, name="chat_session_status", create_type=False,
    values_callable=lambda e: [m.value for m in e],
)

# How long `pending_turn_started_at` (below) is trusted before being treated as stale —
# see that column's docstring. Background turns are hard-limited by
# `settings.chat_turn_timeout_seconds` (default 900s), so the marker outlives that limit by a
# margin: a genuinely slow (but alive) turn is never mistaken for a crashed one, while a process
# that died mid-turn (startup recovery in app/api/v1/chat.py clears those immediately; this is
# the fallback) doesn't wedge the UI in "still working" forever.
STALE_TURN_TIMEOUT_SECONDS = get_settings().chat_turn_timeout_seconds + 300


class ChatSession(UUIDPKMixin, Base):
    __tablename__ = "chat_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[ChatSessionStatus] = mapped_column(
        chat_session_status_enum, nullable=False, default=ChatSessionStatus.ACTIVE
    )
    # Real, AI-generated (or deterministically derived, for a "Refine with assistant"
    # session — see app/api/v1/chat.py::_start_refine_session) short title, set ONCE at
    # session-creation time and never regenerated afterward. NULL only very briefly (a
    # brand-new session row exists for an instant before the title call resolves — see
    # start_chat_session) or if title generation itself failed (best-effort; a session is
    # still fully usable with no title — the frontend falls back to a placeholder). See
    # migration 0006_chat_session_title.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # Why the last background turn failed (see migration 0009_chat_turn_error): a short
    # human-readable reason + a machine code ("bedrock_unavailable" | "timeout" | "interrupted" |
    # "turn_failed"). NULL while a turn runs and after a successful one; cleared when a new turn
    # starts. Read back by the frontend through GET /chat/sessions/{id} (`turn_error`).
    last_turn_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    last_turn_error_code: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    # --- Horizontal scaling: background-turn queue + lease (migration 0011_scaling_leases) ---
    # `pending_turn_started_at` + `turn_message` make the turn a queued job any worker can run. A claim
    # sets `turn_lease_owner` / `turn_lease_expires_at` (renewed ~every 15 s); a lease that expires means
    # the worker died, and the turn is recorded as "interrupted". `turn_cancel_requested_at` is the
    # cross-process cancel flag. All five are cleared whenever the in-progress marker is.
    turn_message: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    turn_first: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    turn_lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True, default=None)
    turn_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    turn_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    turn_cancel_requested_at: Mapped[datetime | None] = mapped_column(
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
