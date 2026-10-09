"""evaluations.cancel_reason: why a job was cancelled by the system (e.g. its chart was trashed)

Revision ID: 0016_eval_cancel_reason
Revises: 0015_chart_trash
Create Date: 2026-10-10

`cancelled_by_trash` marks jobs that were running / queued when their chart went to the trash; restoring the chart
re-queues exactly those rows (see app/trash.py::resume_trash_cancelled_jobs). Nullable, no backfill: older cancelled
rows keep NULL and are never resumed automatically.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_eval_cancel_reason"
down_revision: Union[str, None] = "0015_chart_trash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("cancel_reason", sa.String(40), nullable=True))
    op.execute(
        "CREATE INDEX ix_evaluations_cancel_reason ON evaluations (scorecard_version_id) "
        "WHERE cancel_reason = 'cancelled_by_trash'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evaluations_cancel_reason")
    op.drop_column("evaluations", "cancel_reason")
