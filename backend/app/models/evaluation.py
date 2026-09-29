from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin
from app.models.enums import EvaluationStatus, RagBand

if TYPE_CHECKING:
    from app.models.evaluation_kpi_result import EvaluationKpiResult
    from app.models.scorecard_version import ScorecardVersion
    from app.models.user import User

evaluation_status_enum = ENUM(
    EvaluationStatus, name="evaluation_status", create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
rag_band_enum = ENUM(
    RagBand, name="rag_band", create_type=False, values_callable=lambda e: [m.value for m in e]
)


class Evaluation(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(
            "final_weighted_score IS NULL OR (final_weighted_score >= 0 AND final_weighted_score <= 10)",
            name="ck_evaluations_final_score_range",
        ),
    )

    scorecard_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scorecard_versions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    evaluated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    input_reference: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[EvaluationStatus] = mapped_column(
        evaluation_status_enum, nullable=False, default=EvaluationStatus.PENDING
    )
    final_weighted_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    rag_band: Mapped[RagBand | None] = mapped_column(rag_band_enum, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Denormalized from scorecards.domain (via scorecard_version -> scorecard) for
    # fast filtering/reporting without a join.
    domain: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    scorecard_version: Mapped[ScorecardVersion] = relationship("ScorecardVersion")
    evaluator: Mapped[User] = relationship("User")
    kpi_results: Mapped[list[EvaluationKpiResult]] = relationship(
        "EvaluationKpiResult", back_populates="evaluation", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Evaluation id={self.id} name={self.name!r} status={self.status}>"
