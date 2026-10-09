from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin
from app.models.enums import ScorecardStatus

if TYPE_CHECKING:
    from app.models.scorecard_version import ScorecardVersion
    from app.models.user import User

scorecard_status_enum = ENUM(
    ScorecardStatus, name="scorecard_status", create_type=False, values_callable=lambda e: [m.value for m in e]
)


class Scorecard(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "scorecards"
    __table_args__ = (
        CheckConstraint("target_score IS NULL OR (target_score >= 0 AND target_score <= 10)",
                         name="ck_scorecards_target_score_range"),
        Index("ix_scorecards_trash_owner", "owner_id", postgresql_where=text("deleted_at IS NOT NULL")),
        Index("ix_scorecards_trash_deleted_at", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    domain: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    purpose_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_score: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    status: Mapped[ScorecardStatus] = mapped_column(
        scorecard_status_enum, nullable=False, default=ScorecardStatus.DRAFT
    )
    # Circular reference with scorecard_versions.scorecard_id: the FK constraint itself
    # is added in Alembic via ALTER TABLE after both tables exist (see migration).
    # use_alter=True lets SQLAlchemy's metadata.create_all/drop_all order this safely too.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_versions.id", ondelete="SET NULL", use_alter=True,
                   name="fk_scorecards_current_version_id"),
        nullable=True,
    )

    # Trash (migration 0015): a non-null `deleted_at` means "in the owner's trash"; every access path treats the
    # chart as non-existent until it is restored, and `app.trash.purge_expired` removes it for good after the retention.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    owner: Mapped[User] = relationship("User", back_populates="owned_scorecards", foreign_keys=[owner_id])
    versions: Mapped[list[ScorecardVersion]] = relationship(
        "ScorecardVersion",
        back_populates="scorecard",
        foreign_keys="ScorecardVersion.scorecard_id",
        cascade="all, delete-orphan",
    )
    current_version: Mapped[ScorecardVersion | None] = relationship(
        "ScorecardVersion", foreign_keys=[current_version_id], post_update=True, viewonly=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Scorecard id={self.id} name={self.name!r} status={self.status}>"
