"""AI evaluation pipeline: queue/progress columns, batches, sources, events, jev_raw

Revision ID: 0010_ai_eval_pipeline
Revises: 0009_chat_turn_error
Create Date: 2026-10-04

- `evaluation_status` gains queued / ingesting / processing / scoring. `ALTER TYPE ... ADD VALUE`
  cannot be used in the same transaction as the new value, so it runs inside an autocommit block.
- `evaluations` gains the queue/progress/subject columns; new tables `evaluation_batches`,
  `evaluation_sources`, `evaluation_events`; `evaluation_kpi_results.jev_raw` (JSONB).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_ai_eval_pipeline"
down_revision: Union[str, None] = "0009_chat_turn_error"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_STATUSES = ("queued", "ingesting", "processing", "scoring")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_STATUSES:
            op.execute(f"ALTER TYPE evaluation_status ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        "evaluation_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scorecard_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("source_filename", sa.String(512), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_evaluation_batches_scorecard_id", "evaluation_batches", ["scorecard_id"])

    op.add_column(
        "evaluations",
        sa.Column(
            "batch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_batches.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.add_column("evaluations", sa.Column("source_kind", sa.String(20), nullable=True))
    op.add_column("evaluations", sa.Column("direction_prompt", sa.Text(), nullable=True))
    op.add_column("evaluations", sa.Column("subject_name", sa.String(255), nullable=True))
    op.add_column("evaluations", sa.Column("subject_email", sa.String(320), nullable=True))
    op.add_column("evaluations", sa.Column("stage", sa.String(80), nullable=True))
    op.add_column("evaluations", sa.Column("progress", postgresql.JSONB(), nullable=True))
    op.add_column("evaluations", sa.Column("error_code", sa.String(40), nullable=True))
    op.add_column("evaluations", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("evaluations", sa.Column("sfn_execution_arn", sa.String(1024), nullable=True))
    op.add_column("evaluations", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("evaluations", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("evaluations", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("evaluations", sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_evaluations_batch_id", "evaluations", ["batch_id"])

    op.create_table(
        "evaluation_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evaluation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("original_name", sa.Text(), nullable=True),
        sa.Column("drive_url", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("warnings", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_evaluation_sources_evaluation_id", "evaluation_sources", ["evaluation_id"])

    op.create_table(
        "evaluation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evaluation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_evaluation_events_eval_created", "evaluation_events", ["evaluation_id", "created_at"]
    )

    op.add_column("evaluation_kpi_results", sa.Column("jev_raw", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluation_kpi_results", "jev_raw")
    op.drop_index("ix_evaluation_events_eval_created", table_name="evaluation_events")
    op.drop_table("evaluation_events")
    op.drop_index("ix_evaluation_sources_evaluation_id", table_name="evaluation_sources")
    op.drop_table("evaluation_sources")
    op.drop_index("ix_evaluations_batch_id", table_name="evaluations")
    for col in (
        "attempt", "finished_at", "started_at", "queued_at", "sfn_execution_arn", "error_message",
        "error_code", "progress", "stage", "subject_email", "subject_name", "direction_prompt",
        "source_kind", "batch_id",
    ):
        op.drop_column("evaluations", col)
    op.drop_index("ix_evaluation_batches_scorecard_id", table_name="evaluation_batches")
    op.drop_table("evaluation_batches")

    # Postgres cannot drop enum values: rebuild the type without the AI-pipeline states.
    op.execute("UPDATE evaluations SET status = 'pending' WHERE status = 'queued'")
    op.execute("UPDATE evaluations SET status = 'in_progress' WHERE status IN ('ingesting', 'processing', 'scoring')")
    op.execute("ALTER TABLE evaluations ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE evaluation_status RENAME TO evaluation_status_old")
    op.execute("CREATE TYPE evaluation_status AS ENUM ('pending', 'in_progress', 'completed', 'failed')")
    op.execute(
        "ALTER TABLE evaluations ALTER COLUMN status TYPE evaluation_status "
        "USING status::text::evaluation_status"
    )
    op.execute("ALTER TABLE evaluations ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute("DROP TYPE evaluation_status_old")
