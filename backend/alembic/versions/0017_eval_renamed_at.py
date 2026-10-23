"""evaluations.renamed_at: when a user last renamed the evaluation

Revision ID: 0017_eval_renamed_at
Revises: 0016_eval_cancel_reason
Create Date: 2026-10-23

Set by PATCH /evaluations/{id} when the name changes. A renamed evaluation is labelled with its own name in the Excel
export (instead of the submitter's name), and the AI pipeline never replaces a name a user chose. Nullable, no
backfill: existing rows were never renamed through the app.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_eval_renamed_at"
down_revision: Union[str, None] = "0016_eval_cancel_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("renamed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluations", "renamed_at")
