"""chat_sessions.chat_model, chat_messages.model: the chat model a user picked and the model behind each reply

Revision ID: 0019_chat_models
Revises: 0018_ai_provider_settings
Create Date: 2026-10-26

- `chat_sessions.chat_model`: the OpenRouter model the user last picked in this chat (app/ai/chat_models.py). The
  background worker that runs a queued turn reads it, so the pick reaches the turn without another column. NULL =
  never picked (the admin's default applies) or Bedrock.
- `chat_messages.model`: the model that wrote an assistant reply (the picked chat model, or the scorecard builder's
  model when the builder answered), shown under the reply. NULL for user messages and older replies.
Nullable, no backfill.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_chat_models"
down_revision: Union[str, None] = "0018_ai_provider_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("chat_model", sa.String(200), nullable=True))
    op.add_column("chat_messages", sa.Column("model", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "model")
    op.drop_column("chat_sessions", "chat_model")
