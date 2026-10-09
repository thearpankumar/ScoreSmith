"""Chart sharing, notifications, activity log, usernames, two-role RBAC, list/slot indexes

Revision ID: 0013_sharing_rbac
Revises: 0012_auth_ownership
Create Date: 2026-10-10

- `users`: `username` (unique case-insensitively), `deleted_at` (anonymised tombstone); legacy roles
  member / designer / evaluator become `user` (the `system` marker row and `admin` are untouched).
- New tables `scorecard_collaborators`, `scorecard_invitations`, `scorecard_activity` (append-only: an UPDATE
  trigger rejects changes), `notifications` (unique per-user dedupe key = idempotent creation).
- Indexes for the keyset-paginated evaluations list and for the per-user active-job slot.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_sharing_rbac"
down_revision: Union[str, None] = "0012_auth_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # --- users ---
    op.add_column("users", sa.Column("username", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("deleted_at", _TS, nullable=True))
    op.execute("CREATE UNIQUE INDEX uq_users_username_lower ON users (lower(username)) WHERE username IS NOT NULL")
    op.execute("UPDATE users SET role = 'user' WHERE role NOT IN ('admin', 'user', 'system')")
    op.alter_column("users", "role", server_default="user")

    # --- collaborators / invitations ---
    op.create_table(
        "scorecard_collaborators",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scorecard_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="editor"),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scorecard_id", "user_id", name="uq_scorecard_collaborators_sc_user"),
    )
    op.create_index("ix_scorecard_collaborators_scorecard_id", "scorecard_collaborators", ["scorecard_id"])
    op.create_index("ix_scorecard_collaborators_user_id", "scorecard_collaborators", ["user_id"])

    op.create_table(
        "scorecard_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scorecard_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inviter_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invitee_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="editor"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("responded_at", _TS, nullable=True),
        sa.CheckConstraint("status IN ('pending','accepted','declined','revoked')", name="ck_scorecard_invitations_status"),
    )
    op.create_index("ix_scorecard_invitations_scorecard_id", "scorecard_invitations", ["scorecard_id"])
    op.create_index("ix_scorecard_invitations_invitee_status", "scorecard_invitations", ["invitee_id", "status"])
    op.create_index(
        "uq_scorecard_invitations_pending", "scorecard_invitations", ["scorecard_id", "invitee_id"],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )

    # --- activity log (append-only) ---
    op.create_table(
        "scorecard_activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("scorecard_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_name", sa.String(200), nullable=False, server_default="Someone"),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_scorecard_activity_sc_id", "scorecard_activity", ["scorecard_id", "id"])
    op.execute(
        """
        CREATE FUNCTION scorecard_activity_append_only() RETURNS trigger AS $$
        BEGIN
            -- Tolerated: ON DELETE SET NULL on actor_id, and the deliberate scrub done when an admin deletes a user
            -- (`SET LOCAL qs.allow_activity_scrub = 'on'`, see app/user_admin.py).
            IF current_setting('qs.allow_activity_scrub', true) = 'on' THEN
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.scorecard_id, NEW.actor_name, NEW.action, NEW.entity_type, NEW.entity_id, NEW.summary,
                NEW.detail::text, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.scorecard_id, OLD.actor_name, OLD.action, OLD.entity_type, OLD.entity_id, OLD.summary,
                OLD.detail::text, OLD.created_at) THEN
                RAISE EXCEPTION 'scorecard_activity is append-only';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_scorecard_activity_no_update BEFORE UPDATE ON scorecard_activity "
        "FOR EACH ROW EXECUTE FUNCTION scorecard_activity_append_only()"
    )

    # --- notifications ---
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("read_at", _TS, nullable=True),
    )
    op.create_index(
        "uq_notifications_user_dedupe", "notifications", ["user_id", "dedupe_key"],
        unique=True, postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.execute("CREATE INDEX ix_notifications_user_created ON notifications (user_id, created_at DESC, id DESC)")
    op.create_index(
        "ix_notifications_user_unread", "notifications", ["user_id"], postgresql_where=sa.text("read_at IS NULL")
    )

    # --- evaluations: keyset list + job-slot indexes ---
    op.execute("CREATE INDEX ix_evaluations_created_id ON evaluations (created_at DESC, id DESC)")
    op.execute("CREATE INDEX ix_evaluations_version_created ON evaluations (scorecard_version_id, created_at DESC, id DESC)")
    op.execute(
        "CREATE INDEX ix_evaluations_owner_active ON evaluations (owner_id) "
        "WHERE status IN ('queued','ingesting','processing','scoring')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evaluations_owner_active")
    op.execute("DROP INDEX IF EXISTS ix_evaluations_version_created")
    op.execute("DROP INDEX IF EXISTS ix_evaluations_created_id")
    op.drop_table("notifications")
    op.execute("DROP TRIGGER IF EXISTS trg_scorecard_activity_no_update ON scorecard_activity")
    op.drop_table("scorecard_activity")
    op.execute("DROP FUNCTION IF EXISTS scorecard_activity_append_only()")
    op.drop_table("scorecard_invitations")
    op.drop_table("scorecard_collaborators")
    op.alter_column("users", "role", server_default="member")
    op.execute("DROP INDEX IF EXISTS uq_users_username_lower")
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "username")
