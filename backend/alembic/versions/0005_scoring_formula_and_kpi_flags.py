"""scoring_formula + kpi_nodes.included_in_scoring: optional per-KPI weighting and custom
scoring formulas

Revision ID: 0005_scoring_formula_kpi_flags
Revises: 0004_chat_turn_events
Create Date: 2026-09-29

Note: the revision id is `0005_scoring_formula_kpi_flags` (without "and") rather than
matching this file's own longer filename — `alembic_version.version_num` is
`VARCHAR(32)` (see 0001_initial_schema.py), and the fuller name doesn't fit.

Two additive, independently-optional features (see this pass's task notes):

1. `kpi_nodes.included_in_scoring` (boolean, default true): a KPI can be tracked/scored
   (its value still recorded on an evaluation) WITHOUT contributing to — or being
   constrained by — the sibling weight-sum-to-100 rule. `check_kpi_node_weight_sum` (from
   0001_initial_schema) is redefined here to only SUM/COUNT sibling rows where
   `included_in_scoring = true`; a sibling group with zero included rows is skipped
   entirely (nothing to sum), exactly like the pre-existing "zero remaining rows" case.
   The trigger's own column-list is extended to also fire on UPDATE OF
   included_in_scoring, since toggling the flag can turn a previously-valid group invalid
   (or vice versa) without any row's weight/parent_id/scorecard_version_id changing.

2. `scorecard_versions.scoring_formula` (nullable text): NULL (the default for every
   existing and new row) means "use the classic weighted-average behavior, unchanged" —
   see `app/ai/scoring_formula.py`'s module docstring for the safe-expression-evaluator
   design this backs.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_scoring_formula_kpi_flags"
down_revision: Union[str, None] = "0004_chat_turn_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "kpi_nodes",
        sa.Column("included_in_scoring", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "scorecard_versions",
        sa.Column("scoring_formula", sa.Text(), nullable=True),
    )

    # Redefine the weight-sum trigger function to only sum/require-100 over KPIs where
    # included_in_scoring = true, per sibling group (mirrors 0001_initial_schema's version
    # exactly except for the added `AND included_in_scoring` filters).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_kpi_node_weight_sum() RETURNS TRIGGER AS $$
        DECLARE
            v_parent_id UUID;
            v_scorecard_version_id UUID;
            v_total NUMERIC;
            v_row_count INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_parent_id := OLD.parent_id;
                v_scorecard_version_id := OLD.scorecard_version_id;
            ELSE
                v_parent_id := NEW.parent_id;
                v_scorecard_version_id := NEW.scorecard_version_id;
            END IF;

            IF v_parent_id IS NULL THEN
                SELECT COALESCE(SUM(weight), 0), COUNT(*)
                INTO v_total, v_row_count
                FROM kpi_nodes
                WHERE parent_id IS NULL AND scorecard_version_id = v_scorecard_version_id
                    AND included_in_scoring = TRUE;
            ELSE
                SELECT COALESCE(SUM(weight), 0), COUNT(*)
                INTO v_total, v_row_count
                FROM kpi_nodes
                WHERE parent_id = v_parent_id AND included_in_scoring = TRUE;
            END IF;

            IF v_row_count > 0 AND ABS(v_total - 100) > 0.01 THEN
                RAISE EXCEPTION
                    'kpi_nodes weight sum for parent_id=% (scorecard_version_id=%) is %, expected 100.00 (over KPIs with included_in_scoring=true)',
                    v_parent_id, v_scorecard_version_id, v_total
                    USING ERRCODE = '23514';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_kpi_node_weight_sum ON kpi_nodes;")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_kpi_node_weight_sum
        AFTER INSERT OR UPDATE OF weight, parent_id, scorecard_version_id, included_in_scoring OR DELETE ON kpi_nodes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_kpi_node_weight_sum();
        """
    )


def downgrade() -> None:
    # Restore the pre-0005 trigger function/definition (from 0001_initial_schema) before
    # dropping the column it now depends on.
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_node_weight_sum ON kpi_nodes;")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_kpi_node_weight_sum() RETURNS TRIGGER AS $$
        DECLARE
            v_parent_id UUID;
            v_scorecard_version_id UUID;
            v_total NUMERIC;
            v_row_count INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_parent_id := OLD.parent_id;
                v_scorecard_version_id := OLD.scorecard_version_id;
            ELSE
                v_parent_id := NEW.parent_id;
                v_scorecard_version_id := NEW.scorecard_version_id;
            END IF;

            IF v_parent_id IS NULL THEN
                SELECT COALESCE(SUM(weight), 0), COUNT(*)
                INTO v_total, v_row_count
                FROM kpi_nodes
                WHERE parent_id IS NULL AND scorecard_version_id = v_scorecard_version_id;
            ELSE
                SELECT COALESCE(SUM(weight), 0), COUNT(*)
                INTO v_total, v_row_count
                FROM kpi_nodes
                WHERE parent_id = v_parent_id;
            END IF;

            IF v_row_count > 0 AND ABS(v_total - 100) > 0.01 THEN
                RAISE EXCEPTION
                    'kpi_nodes weight sum for parent_id=% (scorecard_version_id=%) is %, expected 100.00',
                    v_parent_id, v_scorecard_version_id, v_total
                    USING ERRCODE = '23514';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_kpi_node_weight_sum
        AFTER INSERT OR UPDATE OF weight, parent_id, scorecard_version_id OR DELETE ON kpi_nodes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_kpi_node_weight_sum();
        """
    )

    op.drop_column("scorecard_versions", "scoring_formula")
    op.drop_column("kpi_nodes", "included_in_scoring")
