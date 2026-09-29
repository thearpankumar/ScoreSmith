"""chat_sessions: pending_turn_started_at (persistent "turn in progress" marker)

Revision ID: 0003_chat_turn_in_progress
Revises: 0002_ensemble_judge_review
Create Date: 2026-09-29

The chat-turn HTTP endpoints (`POST /chat/sessions[...]`) block synchronously for the
whole LangGraph run today — there is no background job queue. A page refresh mid-turn
(especially relevant with the multi-agent research fan-out, which can take 20-90+
seconds) therefore has nothing server-side to "reconnect" to unless something durable
records that a turn is in flight. This column is that record: set to the turn's start
time right before the graph call begins, cleared to NULL when it ends (success or
failure) — see `app/api/v1/chat.py`'s `_mark_turn_in_progress`/`_clear_turn_in_progress`
and `STALE_TURN_TIMEOUT_SECONDS` (the staleness fallback for a crash that skips the clear).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_chat_turn_in_progress"
down_revision: Union[str, None] = "0002_ensemble_judge_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("pending_turn_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "pending_turn_started_at")
