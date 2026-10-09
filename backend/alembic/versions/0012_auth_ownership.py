"""Authentication + per-user ownership

Revision ID: 0012_auth_ownership
Revises: 0011_scaling_leases
Create Date: 2026-10-09

- `users` gains password / verification / lockout columns (`password_hash` stays NULL for existing rows: they set
  a password through "forgot password" or `python -m app.scripts.set_password`; ownership is NOT rewritten here).
- New tables `refresh_tokens`, `password_reset_tokens`, `oauth_identities`.
- `evaluations.owner_id` (the owning USER; distinct from the lease column `lease_owner`): added nullable,
  backfilled from `evaluated_by`, then made NOT NULL, then indexed - three separate steps.
- `audit_log` gains `ip`, `user_agent`, `request_id`.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_auth_ownership"
down_revision: Union[str, None] = "0011_scaling_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users ---
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("users", sa.Column("failed_logins", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("sessions_valid_after", sa.DateTime(timezone=True), nullable=True))

    # --- token / identity tables ---
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("remember", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False, server_default="reset"),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])

    op.create_table(
        "oauth_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_oauth_identities_provider_subject"),
    )
    op.create_index("ix_oauth_identities_user_id", "oauth_identities", ["user_id"])

    # --- evaluations.owner_id: nullable -> backfill -> NOT NULL -> index ---
    op.add_column(
        "evaluations",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
    )
    op.execute("UPDATE evaluations SET owner_id = evaluated_by WHERE owner_id IS NULL")
    op.alter_column("evaluations", "owner_id", nullable=False)
    op.create_index("ix_evaluations_owner_id", "evaluations", ["owner_id"])
    # Batches are listed / checked by creator.
    op.create_index("ix_evaluation_batches_created_by", "evaluation_batches", ["created_by"])

    # --- audit_log request context ---
    op.add_column("audit_log", sa.Column("ip", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("user_agent", sa.String(300), nullable=True))
    op.add_column("audit_log", sa.Column("request_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "request_id")
    op.drop_column("audit_log", "user_agent")
    op.drop_column("audit_log", "ip")
    op.drop_index("ix_evaluation_batches_created_by", table_name="evaluation_batches")
    op.drop_index("ix_evaluations_owner_id", table_name="evaluations")
    op.drop_column("evaluations", "owner_id")
    op.drop_table("oauth_identities")
    op.drop_table("password_reset_tokens")
    op.drop_table("refresh_tokens")
    for col in ("sessions_valid_after", "locked_until", "failed_logins", "is_active", "email_verified_at", "password_hash"):
        op.drop_column("users", col)
