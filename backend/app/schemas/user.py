from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.schemas.common import ORMBase


class UserCreate(BaseModel):
    email: EmailStr
    name: str
    org_id: uuid.UUID | None = None
    role: str = "member"
    auth_provider_id: str | None = None


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    name: str | None = None
    org_id: uuid.UUID | None = None
    role: str | None = None
    auth_provider_id: str | None = None


class UserRead(ORMBase):
    id: uuid.UUID
    email: str
    name: str
    org_id: uuid.UUID | None
    role: str
    auth_provider_id: str | None
    created_at: datetime
    updated_at: datetime
