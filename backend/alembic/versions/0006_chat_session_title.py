"""chat_sessions.title: a real, AI-generated (or deterministically derived) short title

Revision ID: 0006_chat_session_title
Revises: 0005_scoring_formula_kpi_flags
Create Date: 2026-09-29

Fixes a real gap: `chat_sessions` had no `title` concept at all — the frontend faked one
client-side (truncated raw first message, or "Untitled chat"; see
`frontend/lib/api-client.ts::synthesizeTitle`, removed by this pass). This column is set
ONCE, at session-creation time (see `app/api/v1/chat.py::start_chat_session` /
`_start_refine_session`), by a fast/cheap Bedrock call for a fresh session, or
deterministically (no model call) for a "Refine with assistant" session.

Nullable: a session's title is set immediately after the row is created but is not itself
part of the row's initial INSERT (see start_chat_session), and best-effort title
generation may fail (Bedrock unavailable) without failing the turn — so a NULL title is a
valid, if transient/degraded, state. No backfill needed: existing rows (this project's
DB is empty at the time of writing) simply have `title IS NULL` until their next read
regenerates nothing (titles are never retroactively backfilled by this migration; a truly
old session just falls back to the frontend's placeholder).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_chat_session_title"
down_revision: Union[str, None] = "0005_scoring_formula_kpi_flags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "title")
