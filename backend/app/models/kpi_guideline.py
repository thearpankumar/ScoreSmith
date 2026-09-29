from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.kpi_node import KpiNode


class KpiGuideline(UUIDPKMixin, TimestampMixin, Base):
    """One rung (0-10) of the 11-level qualitative + quantitative guideline for a KPI."""

    __tablename__ = "kpi_guidelines"
    __table_args__ = (
        UniqueConstraint("kpi_node_id", "score_level", name="uq_kpi_guidelines_node_level"),
        CheckConstraint("score_level >= 0 AND score_level <= 10", name="ck_kpi_guidelines_score_level_range"),
    )

    kpi_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kpi_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score_level: Mapped[int] = mapped_column(Integer, nullable=False)
    qualitative_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form structured criteria, e.g. {"metric": "defect_rate", "op": "<=", "value": 2}
    # or {"options": ["fully compliant", "compliant with minor exceptions"]}.
    quantitative_criteria: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    kpi_node: Mapped[KpiNode] = relationship("KpiNode", back_populates="guidelines")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KpiGuideline kpi_node_id={self.kpi_node_id} level={self.score_level}>"
