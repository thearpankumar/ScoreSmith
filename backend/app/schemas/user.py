from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.schemas.common import ORMBase

# A display name is trimmed BEFORE the length check, so a whitespace-only name is rejected rather than stored empty.
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class UserMeUpdate(BaseModel):
    """The only profile field a user can change about themselves. Unknown fields (e.g. `role`) are ignored."""

    name: DisplayName


class UserRead(ORMBase):
    id: uuid.UUID
    email: str
    name: str
    role: str
    username: str | None = None
    email_verified: bool = False
    created_at: datetime


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a `role` field is an error, never silently honoured

    email: EmailStr
    name: DisplayName
    password: str = Field(min_length=1, max_length=1024)


class LoginRequest(BaseModel):
    # Plain str, not EmailStr: seeded accounts such as designer@qualityscorecard.local use a reserved TLD that
    # strict e-mail validation rejects, and sign-in must still work for them. New accounts are validated at signup.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    remember_me: bool = False


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class AuthResponse(BaseModel):
    user: UserRead
    # Also delivered as an httpOnly cookie; returned here so scripts can use `Authorization: Bearer`.
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MessageResponse(BaseModel):
    detail: str


class ProviderInfo(BaseModel):
    id: str
    name: str
    enabled: bool


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]


class RegisterUserRequest(BaseModel):
    """Admin-created user, or the one-time bootstrap of the first admin (role is forced to admin there)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: DisplayName
    password: str = Field(min_length=1, max_length=1024)
    role: Literal["admin", "user"] | None = None


class AuthConfigResponse(BaseModel):
    signup_enabled: bool
    # True only while zero users exist AND a BOOTSTRAP_TOKEN is configured (the first-run setup page).
    setup_required: bool
    # True only outside production when the dev ADMIN_USERNAME shortcut is active: the login form then asks for
    # "Email or username". Always False in production.
    username_login: bool = False
