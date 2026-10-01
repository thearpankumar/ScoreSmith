from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy_utils import LtreeType

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.evaluation_kpi_result import EvaluationKpiResult
    from app.models.kpi_guideline import KpiGuideline
    from app.models.scorecard_version import ScorecardVersion


class KpiNode(UUIDPKMixin, TimestampMixin, Base):
    """A node in the (max depth 4) KPI/parameter hierarchy for one scorecard version.

    `path` is a Postgres `ltree` materialized path (e.g. "root.kpi1.subkpi2"), giving
    O(1) ancestor lookups and fast GiST-indexed subtree queries.

    `weight` is nullable: only LEAF nodes (no other `kpi_nodes` row references this one as
    `parent_id`) carry a weight. A node WITH children is a category/grouping node — purely
    organizational (name + grouping only), never weighted (see migration
    0008_category_nodes_no_weight and `app/ai/draft_schema.py`). Every LEAF `weight` in the
    SAME `scorecard_version_id` (regardless of nesting/category) must sum to 100 —
    enforced by a deferred constraint trigger at the DB layer (see that migration), since a
    plain CHECK constraint cannot aggregate across rows.
    """

    __tablename__ = "kpi_nodes"
    __table_args__ = (
        CheckConstraint("level >= 1 AND level <= 4", name="ck_kpi_nodes_level_range"),
        CheckConstraint("weight >= 0 AND weight <= 100", name="ck_kpi_nodes_weight_range"),
    )

    scorecard_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("kpi_nodes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    path: Mapped[str] = mapped_column(LtreeType, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    weight: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When False, this KPI is tracked/scored (its value is still recorded on an
    # evaluation) but excluded from the sibling weight-sum-to-100 rule AND from the
    # default weighted-average formula's computation (see migration
    # 0005_scoring_formula_and_kpi_flags and app/ai/judge.py::effective_leaf_weights) —
    # i.e. purely informational. Defaults True so every pre-existing KPI is unaffected.
    included_in_scoring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    scorecard_version: Mapped[ScorecardVersion] = relationship(
        "ScorecardVersion", back_populates="kpi_nodes"
    )
    parent: Mapped[KpiNode | None] = relationship(
        "KpiNode", remote_side="KpiNode.id", back_populates="children"
    )
    children: Mapped[list[KpiNode]] = relationship(
        "KpiNode", back_populates="parent", cascade="all, delete-orphan"
    )
    guidelines: Mapped[list[KpiGuideline]] = relationship(
        "KpiGuideline", back_populates="kpi_node", cascade="all, delete-orphan",
        order_by="KpiGuideline.score_level",
    )
    evaluation_results: Mapped[list[EvaluationKpiResult]] = relationship(
        "EvaluationKpiResult", back_populates="kpi_node"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KpiNode id={self.id} name={self.name!r} path={self.path!r} weight={self.weight}>"
