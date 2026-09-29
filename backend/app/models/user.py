from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.scorecard import Scorecard


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # No `organizations` table exists yet in Cycle 1 — plain scalar column, not a FK.
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Full RBAC is deferred (see plan); kept as a free-text role rather than a rigid DB
    # enum so the role vocabulary can evolve without a migration. Convention documented
    # in docs/data_dictionary.md.
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="member")
    auth_provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)

    owned_scorecards: Mapped[list[Scorecard]] = relationship(
        "Scorecard", back_populates="owner", foreign_keys="Scorecard.owner_id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r}>"
