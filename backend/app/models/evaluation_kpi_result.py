from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.evaluation import Evaluation
    from app.models.kpi_node import KpiNode


class EvaluationKpiResult(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_kpi_results"
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 10", name="ck_eval_kpi_results_score_range"),
        CheckConstraint(
            "matched_guideline_level IS NULL OR (matched_guideline_level >= 0 AND matched_guideline_level <= 10)",
            name="ck_eval_kpi_results_matched_level_range",
        ),
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kpi_nodes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    matched_guideline_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_quotes: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)

    # --- Ensemble judge (k=3 calls/KPI, median aggregation) fields — see app/ai/judge.py ---
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score_variance: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    ensemble_raw_scores: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    # AI evaluation pipeline: raw Jev answer {position, probabilities, confidence, noul}.
    jev_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    evaluation: Mapped[Evaluation] = relationship("Evaluation", back_populates="kpi_results")
    kpi_node: Mapped[KpiNode] = relationship("KpiNode", back_populates="evaluation_results")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<EvaluationKpiResult evaluation_id={self.evaluation_id} "
            f"kpi_node_id={self.kpi_node_id} score={self.score}>"
        )
