"""initial schema: extensions, all Cycle 1 tables, indexes, weight-sum trigger

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-28

Idempotent: extensions/enum types use IF NOT EXISTS / checkfirst; safe to run once on a
fresh DB via `alembic upgrade head`. Re-running `upgrade` on an already-migrated DB is not
expected to be run twice (alembic_version guards that) but the extension/trigger statements
themselves are written defensively (CREATE ... IF NOT EXISTS / CREATE OR REPLACE) anyway.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql
from sqlalchemy_utils import LtreeType

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    # --- Extensions ---
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree;")

    # --- Native enum types (created once, referenced with create_type=False below) ---
    scorecard_status = postgresql.ENUM(
        "draft", "published", "archived", name="scorecard_status", create_type=False
    )
    evaluation_status = postgresql.ENUM(
        "pending", "in_progress", "completed", "failed", name="evaluation_status", create_type=False
    )
    rag_band = postgresql.ENUM(
        "band_10_9", "band_8", "band_7", "band_6", "band_5", "band_4", "band_3_0",
        name="rag_band", create_type=False,
    )
    chat_session_status = postgresql.ENUM(
        "active", "completed", "abandoned", name="chat_session_status", create_type=False
    )
    chat_message_role = postgresql.ENUM(
        "user", "assistant", "system", "tool", name="chat_message_role", create_type=False
    )
    audit_action = postgresql.ENUM(
        "create", "update", "delete", name="audit_action", create_type=False
    )

    bind = op.get_bind()
    for enum_type in (
        scorecard_status,
        evaluation_status,
        rag_band,
        chat_session_status,
        chat_message_role,
        audit_action,
    ):
        enum_type.create(bind, checkfirst=True)

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(50), nullable=False, server_default="member"),
        sa.Column("auth_provider_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("auth_provider_id", name="uq_users_auth_provider_id"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # --- scorecards (current_version_id FK added after scorecard_versions exists) ---
    op.create_table(
        "scorecards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("domain", sa.String(120), nullable=True),
        sa.Column("purpose_statement", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("target_score", sa.Numeric(4, 2), nullable=True),
        sa.Column("status", scorecard_status, nullable=False, server_default="draft"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "target_score IS NULL OR (target_score >= 0 AND target_score <= 10)",
            name="ck_scorecards_target_score_range",
        ),
    )
    op.create_index("ix_scorecards_owner_id", "scorecards", ["owner_id"])
    op.create_index("ix_scorecards_domain", "scorecards", ["domain"])

    # --- scorecard_versions ---
    op.create_table(
        "scorecard_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scorecard_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("guideline_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("scorecard_id", "version_number", name="uq_scorecard_version_number"),
    )
    op.create_index("ix_scorecard_versions_scorecard_id", "scorecard_versions", ["scorecard_id"])

    # Deferred circular FK: scorecards.current_version_id -> scorecard_versions.id
    op.create_foreign_key(
        "fk_scorecards_current_version_id",
        "scorecards",
        "scorecard_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- kpi_nodes ---
    op.create_table(
        "kpi_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scorecard_version_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scorecard_versions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "parent_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kpi_nodes.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("path", LtreeType, nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("weight", sa.Numeric(5, 2), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("level >= 1 AND level <= 4", name="ck_kpi_nodes_level_range"),
        sa.CheckConstraint("weight >= 0 AND weight <= 100", name="ck_kpi_nodes_weight_range"),
    )
    op.create_index("ix_kpi_nodes_scorecard_version_id", "kpi_nodes", ["scorecard_version_id"])
    op.create_index("ix_kpi_nodes_parent_id", "kpi_nodes", ["parent_id"])
    op.execute("CREATE INDEX ix_kpi_nodes_path_gist ON kpi_nodes USING GIST (path);")
    # Sibling KPI names must be unique within a scorecard version. Postgres UNIQUE
    # constraints treat NULL as distinct-from-everything, so root nodes (parent_id IS
    # NULL) are normalized via COALESCE to a sentinel UUID for this index only, so two
    # root siblings with the same name are still caught.
    op.execute(
        "CREATE UNIQUE INDEX uq_kpi_nodes_sibling_name ON kpi_nodes "
        "(scorecard_version_id, COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), name);"
    )

    # --- kpi_guidelines ---
    op.create_table(
        "kpi_guidelines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kpi_node_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kpi_nodes.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("score_level", sa.Integer(), nullable=False),
        sa.Column("qualitative_text", sa.Text(), nullable=False),
        sa.Column("quantitative_criteria", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("kpi_node_id", "score_level", name="uq_kpi_guidelines_node_level"),
        sa.CheckConstraint(
            "score_level >= 0 AND score_level <= 10", name="ck_kpi_guidelines_score_level_range"
        ),
    )
    op.create_index("ix_kpi_guidelines_kpi_node_id", "kpi_guidelines", ["kpi_node_id"])

    # --- scorecard_embeddings ---
    op.create_table(
        "scorecard_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scorecard_version_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scorecard_versions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("source_text_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("scorecard_version_id", name="uq_scorecard_embeddings_version_id"),
    )
    op.execute(
        "CREATE INDEX ix_scorecard_embeddings_embedding_hnsw ON scorecard_embeddings "
        "USING hnsw (embedding vector_cosine_ops);"
    )

    # --- evaluations ---
    op.create_table(
        "evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scorecard_version_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scorecard_versions.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "evaluated_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("input_reference", postgresql.JSONB(), nullable=True),
        sa.Column("status", evaluation_status, nullable=False, server_default="pending"),
        sa.Column("final_weighted_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("rag_band", rag_band, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("domain", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "final_weighted_score IS NULL OR (final_weighted_score >= 0 AND final_weighted_score <= 10)",
            name="ck_evaluations_final_score_range",
        ),
    )
    op.create_index("ix_evaluations_scorecard_version_id", "evaluations", ["scorecard_version_id"])
    op.create_index("ix_evaluations_domain", "evaluations", ["domain"])

    # --- evaluation_kpi_results ---
    op.create_table(
        "evaluation_kpi_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evaluation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "kpi_node_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kpi_nodes.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("score", sa.Numeric(4, 2), nullable=False),
        sa.Column("matched_guideline_level", sa.Integer(), nullable=True),
        sa.Column("reasoning_text", sa.Text(), nullable=True),
        sa.Column("evidence_quotes", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("score >= 0 AND score <= 10", name="ck_eval_kpi_results_score_range"),
        sa.CheckConstraint(
            "matched_guideline_level IS NULL OR (matched_guideline_level >= 0 AND matched_guideline_level <= 10)",
            name="ck_eval_kpi_results_matched_level_range",
        ),
    )
    op.create_index("ix_eval_kpi_results_evaluation_id", "evaluation_kpi_results", ["evaluation_id"])
    op.create_index("ix_eval_kpi_results_kpi_node_id", "evaluation_kpi_results", ["kpi_node_id"])

    # A result's kpi_node must belong to the same scorecard_version_id as its evaluation
    # ("cross-version KPI reference" is an invalid-data scenario the framework calls out
    # explicitly). There is no plain FK for this (it spans two tables via a third column),
    # so it is enforced with a BEFORE INSERT/UPDATE trigger.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_eval_kpi_result_version_match() RETURNS TRIGGER AS $$
        DECLARE
            v_eval_version_id UUID;
            v_node_version_id UUID;
        BEGIN
            SELECT scorecard_version_id INTO v_eval_version_id
            FROM evaluations WHERE id = NEW.evaluation_id;

            SELECT scorecard_version_id INTO v_node_version_id
            FROM kpi_nodes WHERE id = NEW.kpi_node_id;

            IF v_eval_version_id IS NULL OR v_node_version_id IS NULL THEN
                RAISE EXCEPTION 'evaluation_kpi_results: evaluation or kpi_node not found'
                    USING ERRCODE = '23514';
            END IF;

            IF v_eval_version_id <> v_node_version_id THEN
                RAISE EXCEPTION
                    'evaluation_kpi_results.kpi_node_id (scorecard_version_id=%) does not belong to '
                    'the same scorecard_version_id as evaluation_id (scorecard_version_id=%)',
                    v_node_version_id, v_eval_version_id
                    USING ERRCODE = '23514';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_eval_kpi_result_version_match ON evaluation_kpi_results;")
    op.execute(
        """
        CREATE TRIGGER trg_eval_kpi_result_version_match
        BEFORE INSERT OR UPDATE OF evaluation_id, kpi_node_id ON evaluation_kpi_results
        FOR EACH ROW
        EXECUTE FUNCTION check_eval_kpi_result_version_match();
        """
    )

    # --- chat_sessions ---
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", chat_session_status, nullable=False, server_default="active"),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column(
            "target_scorecard_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scorecards.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "last_activity_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])

    # --- chat_messages ---
    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", chat_message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", audit_action, nullable=False),
        sa.Column("diff", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])

    # --- weight-sum-to-100-per-sibling-group deferred constraint trigger ---
    # A plain CHECK constraint cannot aggregate across sibling rows, so this is enforced
    # with a PL/pgSQL trigger function + a DEFERRABLE INITIALLY DEFERRED constraint
    # trigger, which only evaluates once per transaction (at COMMIT), so multi-row
    # inserts of a sibling group (the normal way a KPI level is created) work correctly.
    #
    # Root nodes (parent_id IS NULL) are grouped by scorecard_version_id instead, since
    # NULL parent_id alone would otherwise incorrectly group root KPIs across different
    # scorecard versions together.
    #
    # A group with zero remaining rows (e.g. the last sibling was just deleted) is not
    # checked — there is nothing to sum.
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

    op.execute("DROP TRIGGER IF EXISTS trg_kpi_node_weight_sum ON kpi_nodes;")
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_kpi_node_weight_sum
        AFTER INSERT OR UPDATE OF weight, parent_id, scorecard_version_id OR DELETE ON kpi_nodes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION check_kpi_node_weight_sum();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_eval_kpi_result_version_match ON evaluation_kpi_results;")
    op.execute("DROP FUNCTION IF EXISTS check_eval_kpi_result_version_match();")
    op.execute("DROP TRIGGER IF EXISTS trg_kpi_node_weight_sum ON kpi_nodes;")
    op.execute("DROP FUNCTION IF EXISTS check_kpi_node_weight_sum();")

    op.drop_table("audit_log")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_table("evaluation_kpi_results")
    op.drop_table("evaluations")
    op.drop_table("scorecard_embeddings")
    op.drop_table("kpi_guidelines")
    op.drop_table("kpi_nodes")
    op.drop_constraint("fk_scorecards_current_version_id", "scorecards", type_="foreignkey")
    op.drop_table("scorecard_versions")
    op.drop_table("scorecards")
    op.drop_table("users")

    bind = op.get_bind()
    for name in (
        "audit_action",
        "chat_message_role",
        "chat_session_status",
        "rag_band",
        "evaluation_status",
        "scorecard_status",
    ):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)

    op.execute("DROP EXTENSION IF EXISTS ltree;")
    op.execute("DROP EXTENSION IF EXISTS vector;")
