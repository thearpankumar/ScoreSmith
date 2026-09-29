"""ensemble judge: needs_review / score_variance / raw ensemble scores on evaluation_kpi_results

Revision ID: 0002_ensemble_judge_review
Revises: 0001_initial_schema
Create Date: 2026-09-29

The judge now runs k=3 independent Bedrock calls per leaf KPI (guideline-order perturbed
across the 3 calls, not just temperature sampling) and aggregates by median score — see
app/ai/judge.py. This migration adds the columns needed to persist that aggregation
outcome so a human reviewer can see *why* a KPI was flagged, not just that it was:

- `needs_review` (bool, NOT NULL default false): true when the 3 calls disagreed enough
  to warrant a human look (score spread > 2 points across the 3 calls, OR the matched
  guideline level wasn't unanimous).
- `score_variance` (numeric(4,2), nullable): the score spread (max - min) across the 3
  ensemble calls for this KPI. Null for any pre-ensemble result (none exist yet in this
  build, but the column stays nullable for forward compatibility with non-ensemble paths,
  e.g. a future single-call debug mode).
- `ensemble_raw_scores` (JSONB, nullable): the raw per-call `{matched_level, score}`
  list (length k=3), kept for transparency/audit — this is what a reviewer or a live
  verification step inspects to see the actual disagreement, not just the aggregate.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_ensemble_judge_review"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_kpi_results",
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "evaluation_kpi_results",
        sa.Column("score_variance", sa.Numeric(4, 2), nullable=True),
    )
    op.add_column(
        "evaluation_kpi_results",
        sa.Column("ensemble_raw_scores", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_eval_kpi_results_needs_review",
        "evaluation_kpi_results",
        ["needs_review"],
        postgresql_where=sa.text("needs_review = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_eval_kpi_results_needs_review", table_name="evaluation_kpi_results")
    op.drop_column("evaluation_kpi_results", "ensemble_raw_scores")
    op.drop_column("evaluation_kpi_results", "score_variance")
    op.drop_column("evaluation_kpi_results", "needs_review")
