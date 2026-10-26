"""chat_attachments: files a user attaches to a chat message (PDF, DOCX, XLSX, CSV, images, text)

Revision ID: 0020_chat_attachments
Revises: 0019_chat_models
Create Date: 2026-10-26

One row per uploaded file (app/api/v1/chat_attachments.py). The browser uploads the original straight to S3 under
`chat/{user_id}/{id}.{ext}`; on completion the text is extracted once (PDF / DOCX in the extraction Lambda,
spreadsheets / text on the backend, images described by the builder model) and kept here, so every later model
call reads the text instead of re-parsing the file. A row is bound to the user message it was sent with
(`message_id`, set on send); unbound rows are drafts the user may still remove. Deleting the chat session or the
message deletes its attachments; deleting the user deletes everything.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_chat_attachments"
down_revision: Union[str, None] = "0019_chat_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "chat_attachments",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("outline", postgresql.JSONB(), nullable=True),
        sa.Column("token_estimate", sa.Integer(), nullable=True),
        sa.Column("needs_native", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'processing', 'ready', 'failed')", name="ck_chat_attachments_status"),
        sa.CheckConstraint(
            "kind IN ('pdf', 'docx', 'xlsx', 'csv', 'image', 'text')", name="ck_chat_attachments_kind"
        ),
    )
    op.create_index("ix_chat_attachments_user_created", "chat_attachments", ["user_id", "created_at"])
    op.create_index("ix_chat_attachments_session", "chat_attachments", ["session_id"])
    op.create_index("ix_chat_attachments_message", "chat_attachments", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_attachments_message", table_name="chat_attachments")
    op.drop_index("ix_chat_attachments_session", table_name="chat_attachments")
    op.drop_index("ix_chat_attachments_user_created", table_name="chat_attachments")
    op.drop_table("chat_attachments")
