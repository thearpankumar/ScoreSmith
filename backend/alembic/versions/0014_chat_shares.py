"""Chat sharing (read-only shares of a chat, optionally together with its chart)

Revision ID: 0014_chat_shares
Revises: 0013_sharing_rbac
Create Date: 2026-10-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_chat_shares"
down_revision: Union[str, None] = "0013_sharing_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("with_chart", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("saved_scorecard_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scorecards.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "recipient_id", name="uq_chat_shares_session_recipient"),
    )
    op.create_index("ix_chat_shares_session_id", "chat_shares", ["session_id"])
    op.create_index("ix_chat_shares_recipient_id", "chat_shares", ["recipient_id"])


def downgrade() -> None:
    op.drop_table("chat_shares")
