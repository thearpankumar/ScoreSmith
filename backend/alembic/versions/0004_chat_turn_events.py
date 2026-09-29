"""chat_turn_events: granular live-trace event log for the chat-builder AI pipeline

Revision ID: 0004_chat_turn_events
Revises: 0003_chat_turn_in_progress
Create Date: 2026-09-29

Replaces the generic "Assistant is thinking…" indicator with a real, granular, persisted
trace of what the AI pipeline is doing turn by turn — including, when the multi-agent
research fan-out (`research_kpis` / `_run_research_agent` in
`app/ai/scorecard_builder.py`) spawns concurrent research agents, what EACH individual
agent is doing. Mirrors `chat_sessions.pending_turn_started_at`'s own "durable marker a
page refresh can read back" design (see 0003_chat_turn_in_progress) rather than replacing
it: `turn_started_at` here correlates every event to the exact turn attempt whose
`pending_turn_started_at` value matches, so a reader can scope "just the current turn's
events" without touching the session's whole history.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_chat_turn_events"
down_revision: Union[str, None] = "0003_chat_turn_in_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_turn_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_turn_events_session_id", "chat_turn_events", ["session_id"])
    op.create_index(
        "ix_chat_turn_events_session_turn",
        "chat_turn_events",
        ["session_id", "turn_started_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_turn_events_session_turn", table_name="chat_turn_events")
    op.drop_index("ix_chat_turn_events_session_id", table_name="chat_turn_events")
    op.drop_table("chat_turn_events")
