"""chat_turn_events.round: distinguishes which research round an event belongs to

Revision ID: 0007_chat_turn_event_round
Revises: 0006_chat_session_title
Create Date: 2026-09-29

Part of the "iterative/multi-round research" feature: `research_kpis` (see
`app/ai/scorecard_builder.py`) can now run more than one bounded round of the multi-agent
research fan-out when the master isn't yet confident coverage is sufficient (see
`MAX_RESEARCH_ROUNDS`/`_assess_research_coverage`). The live-trace UI (`TurnTraceCard.tsx`)
needs to group/label events by round distinctly, so this adds a plain integer `round`
column to `chat_turn_events` rather than overloading `actor` (which stays exactly as it
was: `"master"` or `"research_agent_{i+1}"`, index-based WITHIN a round).

`NOT NULL DEFAULT 1`: every event written before this feature existed (and every event
outside the research fan-out, e.g. all of `propose_kpis`'s own master events) is
unambiguously "round 1" — no backfill logic needed since this project's DB is empty at
the time of writing (see the 0006 migration's own note), and even on a populated DB the
straightforward default is correct for 100% of pre-existing rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_chat_turn_event_round"
down_revision: Union[str, None] = "0006_chat_session_title"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_turn_events",
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("chat_turn_events", "round")
