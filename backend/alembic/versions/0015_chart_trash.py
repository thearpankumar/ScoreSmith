"""Chart trash (soft delete): scorecards.deleted_at / deleted_by

Revision ID: 0015_chart_trash
Revises: 0014_chat_shares
Create Date: 2026-10-10

A trashed chart keeps every row (versions, evaluations, collaborators) but is invisible to every access path until
the owner restores it or it is purged after `TRASH_RETENTION_DAYS`. Two partial indexes keep the trash listing
(owner_id) and the retention purge (deleted_at) cheap while the normal listing is unaffected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_chart_trash"
down_revision: Union[str, None] = "0014_chat_shares"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scorecards", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "scorecards",
        sa.Column("deleted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.execute("CREATE INDEX ix_scorecards_trash_owner ON scorecards (owner_id) WHERE deleted_at IS NOT NULL")
    op.execute("CREATE INDEX ix_scorecards_trash_deleted_at ON scorecards (deleted_at) WHERE deleted_at IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_scorecards_trash_deleted_at")
    op.execute("DROP INDEX IF EXISTS ix_scorecards_trash_owner")
    op.drop_column("scorecards", "deleted_by")
    op.drop_column("scorecards", "deleted_at")
