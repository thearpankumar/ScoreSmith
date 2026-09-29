from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.kpi_node import KpiNode
    from app.models.scorecard import Scorecard
    from app.models.scorecard_embedding import ScorecardEmbedding


class ScorecardVersion(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "scorecard_versions"
    __table_args__ = (
        UniqueConstraint("scorecard_id", "version_number", name="uq_scorecard_version_number"),
    )

    scorecard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    guideline_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL (default, every pre-existing row) = the classic weighted-average formula,
    # unchanged. Non-null = an arbitrary expression referencing kpi["KPI Name"] scores,
    # safely evaluated by app/ai/scoring_formula.py. See migration
    # 0005_scoring_formula_and_kpi_flags.
    scoring_formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    scorecard: Mapped[Scorecard] = relationship(
        "Scorecard", back_populates="versions", foreign_keys=[scorecard_id]
    )
    kpi_nodes: Mapped[list[KpiNode]] = relationship(
        "KpiNode", back_populates="scorecard_version", cascade="all, delete-orphan"
    )
    embedding: Mapped[ScorecardEmbedding | None] = relationship(
        "ScorecardEmbedding", back_populates="scorecard_version", cascade="all, delete-orphan",
        uselist=False,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ScorecardVersion id={self.id} scorecard_id={self.scorecard_id} v={self.version_number}>"
