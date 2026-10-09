from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, select
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin
from app.models.enums import EvaluationStatus, RagBand
from app.models.user import User

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
    # The USER that owns this evaluation (migration 0012). Always set server-side from the authenticated
    # user; defaults to `evaluated_by` for code that builds rows directly (scripts, tests).
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True,
        default=lambda ctx: ctx.get_current_parameters()["evaluated_by"],
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

    # --- AI evaluation pipeline (migration 0010_ai_eval_pipeline) ---
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)  # upload | drive | mixed
    direction_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sfn_execution_arn: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # --- Horizontal scaling: driver lease (migration 0011_scaling_leases) ---
    # The process driving an active evaluation owns it through `lease_owner` until `lease_expires_at`,
    # renewing both (and `heartbeat_at`) about every 15 s. Another process may adopt the evaluation only
    # once the lease has expired (or was released on a clean shutdown). NOT the user-ownership column.
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by the API process on cancel; whichever process drives the evaluation sees it and stops.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why the SYSTEM cancelled the job (migration 0016): "cancelled_by_trash" = its chart was trashed; restoring the
    # chart re-queues those rows. After that it becomes "trash_restore_handled" so a later trash cycle never repeats it.
    cancel_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    scorecard_version: Mapped[ScorecardVersion] = relationship("ScorecardVersion")
    evaluator: Mapped[User] = relationship("User", foreign_keys=[evaluated_by])
    kpi_results: Mapped[list[EvaluationKpiResult]] = relationship(
        "EvaluationKpiResult", back_populates="evaluation", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Evaluation id={self.id} name={self.name!r} status={self.status}>"


# The runner's display name, so a list of a SHARED chart's evaluations can label who ran each one without an extra
# lookup per row (read-only, loaded with the row).
Evaluation.runner_name = column_property(  # type: ignore[attr-defined]
    select(User.name).where(User.id == Evaluation.owner_id).correlate_except(User).scalar_subquery()
)
