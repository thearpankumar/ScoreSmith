"""chat_sessions: last_turn_error (why the last background turn failed)

Revision ID: 0009_chat_turn_error
Revises: 0008_category_nodes_no_weight
Create Date: 2026-10-03

Chat turns now run as background tasks (POST returns 202 immediately; see
`app/api/v1/chat.py`), so a failure can no longer be reported in the POST response. The
frontend polls `GET /chat/sessions/{id}` instead and reads this column through
`ChatTurnRead.turn_error`: NULL while a turn runs or after a successful one, a short
human-readable reason after a failed/interrupted turn (Bedrock unavailable, timeout, server
restart). Cleared at the start of every new turn. Additive and nullable, so it is safe to apply
online (`alembic upgrade head`) and to downgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_chat_turn_error"
down_revision: Union[str, None] = "0008_category_nodes_no_weight"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("last_turn_error", sa.Text(), nullable=True))
    # Keep the "turn_error is 'unavailable' vs generic" distinction without a second column.
    op.add_column("chat_sessions", sa.Column("last_turn_error_code", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "last_turn_error_code")
    op.drop_column("chat_sessions", "last_turn_error")
