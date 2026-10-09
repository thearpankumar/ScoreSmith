"""Horizontal scaling: driver leases, cross-process cancel flags, chat-turn queue, idempotency keys

Revision ID: 0011_scaling_leases
Revises: 0010_ai_eval_pipeline
Create Date: 2026-10-09

- `evaluations` gains `lease_owner`, `lease_expires_at`, `heartbeat_at`, `cancel_requested_at`. (The lease
  owner column is deliberately NOT called `owner_id`: that name is reserved for the owning USER.)
- `chat_sessions` gains the same lease/cancel fields (prefixed `turn_`) plus `turn_message` / `turn_first`,
  which turn the in-progress marker into a queued job any worker can claim.
- New table `idempotency_keys` (Idempotency-Key header on evaluation / batch creation).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_scaling_leases"
down_revision: Union[str, None] = "0010_ai_eval_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("lease_owner", sa.String(120), nullable=True))
    op.add_column("evaluations", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("evaluations", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("evaluations", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("chat_sessions", sa.Column("turn_message", sa.Text(), nullable=True))
    op.add_column("chat_sessions", sa.Column("turn_first", sa.Boolean(), nullable=True))
    op.add_column("chat_sessions", sa.Column("turn_lease_owner", sa.String(120), nullable=True))
    op.add_column("chat_sessions", sa.Column("turn_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_sessions", sa.Column("turn_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_sessions", sa.Column("turn_cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    # Workers poll for unclaimed / expired turns: keep that scan on the (few) in-progress rows only.
    op.create_index(
        "ix_chat_sessions_pending_turn", "chat_sessions", ["pending_turn_started_at"],
        postgresql_where=sa.text("pending_turn_started_at IS NOT NULL"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(40), primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_index("ix_chat_sessions_pending_turn", table_name="chat_sessions")
    for col in (
        "turn_cancel_requested_at", "turn_heartbeat_at", "turn_lease_expires_at", "turn_lease_owner",
        "turn_first", "turn_message",
    ):
        op.drop_column("chat_sessions", col)
    for col in ("cancel_requested_at", "heartbeat_at", "lease_expires_at", "lease_owner"):
        op.drop_column("evaluations", col)
