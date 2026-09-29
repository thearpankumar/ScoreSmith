from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.scorecard_version import ScorecardVersion

# Titan Text Embeddings V2 output dimension, per the plan.
EMBEDDING_DIM = 1024


class ScorecardEmbedding(UUIDPKMixin, Base):
    """Similarity-search vector for one scorecard version's purpose/KPI text, used to
    suggest reuse of an existing scorecard for a new request. HNSW-indexed (cosine)."""

    __tablename__ = "scorecard_embeddings"

    scorecard_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_versions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    source_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    scorecard_version: Mapped[ScorecardVersion] = relationship(
        "ScorecardVersion", back_populates="embedding"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ScorecardEmbedding scorecard_version_id={self.scorecard_version_id}>"
