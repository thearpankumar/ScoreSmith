from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin
from app.models.enums import AuditAction

audit_action_enum = ENUM(
    AuditAction, name="audit_action", create_type=False, values_callable=lambda e: [m.value for m in e]
)


class AuditLog(UUIDPKMixin, Base):
    __tablename__ = "audit_log"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action: Mapped[AuditAction] = mapped_column(audit_action_enum, nullable=False)
    diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Request context (migration 0012). Auth events carry their name in `diff["event"]` (the `action` enum
    # stays create/update/delete: login = create, logout = delete, password reset = update).
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog entity_type={self.entity_type} entity_id={self.entity_id} action={self.action}>"
