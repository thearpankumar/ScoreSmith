"""category nodes carry no weight: only LEAF kpi_nodes are weighted, and the weight-sum
rule is now enforced GLOBALLY over every leaf in a scorecard version (not per immediate
sibling group)

Revision ID: 0008_category_nodes_no_weight
Revises: 0007_chat_turn_event_round
Create Date: 2026-10-01

**Why**: the recent KPI-category restructure (migration-free — categories are just
`level=1` `kpi_nodes` with children, see `app/ai/draft_schema.py`) accidentally required
every CATEGORY node to also carry its own weight, with categories summing to 100 among
themselves at the root AND each category's children separately summing to 100 within that
category — a two-level multiplicative weighting scheme. Categories are meant to be purely
organizational groupings (name + grouping only, no numeric role) — the user/model should
never have to think about "how much does the Quality category weigh" at all.

**What changes**:
1. `kpi_nodes.weight` becomes nullable — a node that has children (a category/grouping
   node, at ANY depth) stores `weight = NULL` and plays no part in any weight-sum
   constraint. `ck_kpi_nodes_weight_range` is untouched: a Postgres CHECK constraint
   already treats a NULL value as satisfying the check, so no NULL-handling change is
   needed there.
2. `check_kpi_node_weight_sum()` is redefined so the group it sums/validates is no longer
   "this row's immediate siblings" — it is now "every LEAF node (a kpi_nodes row with no
   children of its own) in the same `scorecard_version_id`, regardless of how deep it sits
   or which category/subcategory it's nested under". A non-leaf (category) row is excluded
   from the sum entirely, at any level — it is never required to have a weight, and never
   constrains or is constrained by this rule.
   - This is a strict generalization of the pre-existing "root siblings grouped by
     scorecard_version_id" case: a flat scorecard (no categories at all — every KPI sits
     directly at the root with no children) behaves IDENTICALLY to before, since every
     root KPI already was both "a root sibling" and "a leaf" — see
     tests/test_weight_trigger.py's pre-existing tests, all still green unmodified.
   - For a scorecard WITH categories, this means a leaf's `weight` now directly represents
     its final share of the WHOLE scorecard (no more multiplying a leaf's in-category
     weight by its category's own weight — see `app/ai/judge.py::effective_leaf_weights`,
     updated in the same pass to match), so every leaf across every category must
     collectively sum to 100 rather than each category's leaves separately summing to 100.
   - `included_in_scoring = false` leaves are still excluded from the sum, same as before
     (migration 0005_scoring_formula_and_kpi_flags).
3. Data backfill: legacy categorized rows (if any) are converted so every leaf keeps its
   effective share (own weight x ancestors' weight/100) and category weights become NULL;
   `downgrade()` converts back the same way, preserving effective shares. Both are no-ops
   when no nested nodes exist.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_category_nodes_no_weight"
down_revision: Union[str, None] = "0007_chat_turn_event_round"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old trigger before any DML so no deferred events are pending when the
    # ALTER below runs (Postgres refuses to ALTER a table with pending trigger events).
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_node_weight_sum ON kpi_nodes;")
    op.alter_column("kpi_nodes", "weight", nullable=True)

    # Backfill legacy categorized data (a no-op on a DB with no nested nodes): a leaf's
    # new weight is its effective share under the old multiplicative scheme (its own weight
    # times each ancestor's weight/100), and every non-leaf node's weight becomes NULL.
    op.execute(
        """
        WITH RECURSIVE eff AS (
            SELECT id, weight::numeric AS w FROM kpi_nodes WHERE parent_id IS NULL
            UNION ALL
            SELECT k.id, e.w * k.weight / 100
            FROM kpi_nodes k JOIN eff e ON k.parent_id = e.id
        )
        UPDATE kpi_nodes k
        SET weight = ROUND(eff.w, 2)
        FROM eff
        WHERE k.id = eff.id
          AND k.parent_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM kpi_nodes c WHERE c.parent_id = k.id);
        """
    )
    op.execute(
        "UPDATE kpi_nodes SET weight = NULL "
        "WHERE EXISTS (SELECT 1 FROM kpi_nodes c WHERE c.parent_id = kpi_nodes.id);"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_kpi_node_weight_sum() RETURNS TRIGGER AS $$
        DECLARE
            v_scorecard_version_id UUID;
            v_total NUMERIC;
            v_row_count INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_scorecard_version_id := OLD.scorecard_version_id;
            ELSE
                v_scorecard_version_id := NEW.scorecard_version_id;
            END IF;

            -- Only LEAF nodes (no row in kpi_nodes references them as parent_id) with
            -- included_in_scoring=true participate — a category/grouping node (has
            -- children) is purely organizational and carries no weight of its own at any
            -- depth. Every such leaf in the WHOLE scorecard version is now one group
            -- (not one group per immediate parent) — see this migration's own docstring.
            SELECT COALESCE(SUM(k.weight), 0), COUNT(*)
            INTO v_total, v_row_count
            FROM kpi_nodes k
            WHERE k.scorecard_version_id = v_scorecard_version_id
              AND k.included_in_scoring = TRUE
              AND NOT EXISTS (SELECT 1 FROM kpi_nodes c WHERE c.parent_id = k.id);

            IF v_row_count > 0 AND ABS(v_total - 100) > 0.01 THEN
                RAISE EXCEPTION
                    'kpi_nodes leaf weight sum for scorecard_version_id=% is %, expected 100.00 (over leaf KPIs with included_in_scoring=true; category/grouping nodes carry no weight)',
                    v_scorecard_version_id, v_total
                    USING ERRCODE = '23514';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # Same trigger definition as before (0005); only the function body changed.
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
    # The weight-sum trigger MUST be dropped before any DML below: its deferred events from
    # the UPDATE would otherwise still be pending when `ALTER COLUMN ... SET NOT NULL`
    # runs, and Postgres refuses to ALTER a table with pending trigger events.
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_node_weight_sum ON kpi_nodes;")

    # Convert back to the old two-level scheme while preserving every leaf's EFFECTIVE
    # share: a non-leaf node's weight = the sum of its descendant leaves' weights, and every
    # non-root node's weight = its share of its parent's subtree (so each sibling group
    # sums to 100). Flat (root-only) scorecards are untouched. Values are rounded to the
    # column's numeric(5,2), so a sibling group may be off by a few hundredths.
    op.execute(
        """
        WITH subtree AS (
            SELECT n.id,
                   COALESCE(SUM(l.weight) FILTER (WHERE l.included_in_scoring), 0) AS s
            FROM kpi_nodes n
            JOIN kpi_nodes l
              ON l.scorecard_version_id = n.scorecard_version_id
             AND l.path <@ n.path
             AND NOT EXISTS (SELECT 1 FROM kpi_nodes c WHERE c.parent_id = l.id)
            GROUP BY n.id
        ),
        sib AS (
            SELECT id, COUNT(*) OVER (PARTITION BY scorecard_version_id, parent_id) AS n_sib
            FROM kpi_nodes
        )
        UPDATE kpi_nodes k
        SET weight = ROUND(
            CASE
                WHEN k.parent_id IS NULL THEN st.s
                WHEN pst.s > 0 THEN st.s / pst.s * 100
                ELSE 100.0 / sib.n_sib
            END, 2)
        FROM subtree st
        LEFT JOIN subtree pst ON pst.id = (SELECT parent_id FROM kpi_nodes WHERE id = st.id)
        JOIN sib ON sib.id = st.id
        WHERE k.id = st.id
          AND k.included_in_scoring
          AND (k.weight IS NULL OR k.parent_id IS NOT NULL);
        """
    )
    # Anything still NULL (e.g. an excluded category) gets the old safety-net default.
    op.execute("UPDATE kpi_nodes SET weight = 0 WHERE weight IS NULL;")
    op.alter_column("kpi_nodes", "weight", nullable=False)

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
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_kpi_node_weight_sum
        AFTER INSERT OR UPDATE OF weight, parent_id, scorecard_version_id, included_in_scoring OR DELETE ON kpi_nodes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_kpi_node_weight_sum();
        """
    )
