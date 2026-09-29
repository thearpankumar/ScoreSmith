from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.chat_session import ChatSession


class ChatTurnEvent(Base, UUIDPKMixin):
    """A single granular step of the AI pipeline's work on ONE chat turn — the persisted
    source of truth behind the "what is the assistant doing right now" live trace UI
    (replaces the old generic "Assistant is thinking…" indicator).

    Mirrors `ChatSession.pending_turn_started_at`'s own "durable marker a page refresh can
    read back" design (see that column's docstring and `app/api/v1/chat.py`'s
    `_mark_turn_in_progress`/`_clear_turn_in_progress`): the chat-turn HTTP endpoints block
    synchronously for the whole LangGraph run, so live visibility into a turn in progress
    — including each concurrently-running research agent's own activity — has nowhere to
    live except a table the frontend can poll and a page refresh can re-fetch.

    - `turn_started_at` correlates every event to the specific turn attempt it belongs to
      (the exact same timestamp value written to `ChatSession.pending_turn_started_at` for
      that turn) — this is how a reader scopes "give me only the CURRENT/most recent
      turn's events", not the whole session's history.
    - `actor` is a stable per-turn identifier for whichever part of the pipeline produced
      the event: `"master"` for the orchestrator (`research_kpis`'s angle-deciding step and
      `propose_kpis`'s own consolidate/propose/confirm steps), or `"research_agent_{i+1}"`
      (index-based, assigned once per concurrent `_run_research_agent` invocation) for one
      of the concurrently-running research workers — see `scorecard_builder.py`.
    - `message` is the actual human-readable text the frontend renders verbatim (e.g.
      `"Searching: 'ISO 27001 vendor security certification requirements'"`) — NOT just the
      bare `event_type` code.

    Writes are best-effort (see `app/ai/turn_events.py::emit_turn_event`): a failure to
    write one of these must never break the real chat turn it's describing.
    """

    __tablename__ = "chat_turn_events"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[ChatSession] = relationship("ChatSession")

    __table_args__ = (
        # The read path (GET /chat/sessions/{id}/turn-events) always filters by
        # (session_id, turn_started_at) and orders by created_at — a composite index makes
        # that scoped-to-the-current-turn query cheap even once a long-lived session has
        # accumulated many past turns' worth of rows.
        Index("ix_chat_turn_events_session_turn", "session_id", "turn_started_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatTurnEvent id={self.id} actor={self.actor} event_type={self.event_type}>"
