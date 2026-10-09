from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.scorecard import Scorecard


# Role model. "admin" may manage users (POST /auth/register-user); every other value is an ordinary user. Legacy
# rows may carry other free-text roles (e.g. "designer"): they are treated as ordinary users. The role is never
# accepted from signup / PATCH /me - only an admin (or the one-time bootstrap) can grant "admin".
# Exactly two roles for people: admin and user (migration 0013 normalised legacy member/designer/evaluator to user).
# The seeded system marker row is not a person and is never assignable.
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_MEMBER = ROLE_USER  # legacy alias
ROLE_SYSTEM = "system"
ASSIGNABLE_ROLES = (ROLE_ADMIN, ROLE_USER)


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # No `organizations` table exists yet in Cycle 1 — plain scalar column, not a FK.
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Full RBAC is deferred (see plan); kept as a free-text role rather than a rigid DB
    # enum so the role vocabulary can evolve without a migration. Convention documented
    # in docs/data_dictionary.md.
    role: Mapped[str] = mapped_column(String(50), nullable=False, default=ROLE_USER)
    auth_provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)


    # --- Sharing / RBAC (migration 0013_sharing_rbac) ---
    # Optional handle, unique case-insensitively; sign-in and chart invitations accept it as well as the email.
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set when an admin deleted the account: the row stays as an anonymised tombstone (FKs keep working).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Authentication (migration 0012_auth_ownership) ---
    # Argon2id PHC string. NULL for accounts that have never set a password (seeded rows, OAuth-only sign-ups);
    # such an account cannot log in with a password until it completes "forgot password".
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Access tokens issued before this instant are rejected (set by logout-all and password reset).
    sessions_valid_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owned_scorecards: Mapped[list[Scorecard]] = relationship(
        "Scorecard", back_populates="owner", foreign_keys="Scorecard.owner_id"
    )

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r}>"
