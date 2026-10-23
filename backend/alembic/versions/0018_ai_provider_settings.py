"""AI provider setting (Bedrock | OpenRouter): app_settings, evaluations.ai_provider, embeddings per model

Revision ID: 0018_ai_provider_settings
Revises: 0017_eval_renamed_at
Create Date: 2026-10-23

- `app_settings`: product-wide settings an admin changes at runtime. The AI provider is stored under the key
  "ai_provider" (see app/ai/providers.py). No row = no explicit choice (the available provider is used).
- `evaluations.ai_provider`: the provider an evaluation's run started on, so it finishes there even if an admin
  switches mid-run. Nullable, no backfill.
- `scorecard_embeddings`: one row per (version, embedding_model) instead of one per version, so Titan vectors
  (Bedrock) and text-embedding-3-small vectors (OpenRouter) live side by side. The downgrade keeps only the newest
  row per version before restoring the old one-per-version constraint.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_ai_provider_settings"
down_revision: Union[str, None] = "0017_eval_renamed_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.UniqueConstraint("key", name="uq_app_settings_key"),
    )

    op.add_column("evaluations", sa.Column("ai_provider", sa.String(20), nullable=True))

    op.drop_constraint("uq_scorecard_embeddings_version_id", "scorecard_embeddings", type_="unique")
    op.create_unique_constraint(
        "uq_scorecard_embeddings_version_model", "scorecard_embeddings", ["scorecard_version_id", "embedding_model"]
    )


def downgrade() -> None:
    # Keep one embedding per version so the old unique constraint can come back: the Bedrock (Titan) row when there is
    # one, since the pre-0018 code embeds only with Titan and never re-embeds an unchanged text (an OpenAI-space vector
    # left behind would be compared with Titan query vectors forever); otherwise the most recently updated row.
    op.execute(
        """
        DELETE FROM scorecard_embeddings e
        USING (
            SELECT id, row_number() OVER (
                PARTITION BY scorecard_version_id
                ORDER BY (left(embedding_model, 7) = 'amazon.') DESC, updated_at DESC, created_at DESC, id
            ) AS rn
            FROM scorecard_embeddings
        ) ranked
        WHERE e.id = ranked.id AND ranked.rn > 1
        """
    )
    op.drop_constraint("uq_scorecard_embeddings_version_model", "scorecard_embeddings", type_="unique")
    op.create_unique_constraint("uq_scorecard_embeddings_version_id", "scorecard_embeddings", ["scorecard_version_id"])

    op.drop_column("evaluations", "ai_provider")
    op.drop_table("app_settings")
