from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin

ATTACHMENT_KINDS = ("pdf", "docx", "xlsx", "csv", "image", "text")
ATTACHMENT_STATUSES = ("pending", "processing", "ready", "failed")


class ChatAttachment(UUIDPKMixin, Base):
    """A file a user attached to a chat message (migration 0020, app/api/v1/chat_attachments.py).

    The original lives in S3 at `s3_key`; its text is extracted once, on upload completion, into `extracted_text`
    (with an `outline`: pages / sheets / rows / truncation) so model calls never re-parse the file. `needs_native`
    marks a scanned PDF whose pages the chat model should see as a file rather than as text. Bound to the user message
    it was sent with (`message_id`) when the message is sent; until then it belongs to `user_id` only."""

    __tablename__ = "chat_attachments"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'ready', 'failed')", name="ck_chat_attachments_status"),
        CheckConstraint("kind IN ('pdf', 'docx', 'xlsx', 'csv', 'image', 'text')", name="ck_chat_attachments_kind"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    outline: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    needs_native: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatAttachment id={self.id} kind={self.kind} status={self.status}>"
