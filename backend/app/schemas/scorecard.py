from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ScorecardStatus
from app.schemas.common import ORMBase
from app.schemas.kpi import KpiNodeReadWithGuidelines


class ScorecardCreate(BaseModel):
    name: str
    domain: str | None = None
    purpose_statement: str | None = None
    scope: str | None = None
    target_score: float | None = Field(default=None, ge=0, le=10)
    status: ScorecardStatus = ScorecardStatus.DRAFT


class ScorecardUpdate(BaseModel):
    name: str | None = None
    domain: str | None = None
    purpose_statement: str | None = None
    scope: str | None = None
    target_score: float | None = Field(default=None, ge=0, le=10)
    status: ScorecardStatus | None = None
    current_version_id: uuid.UUID | None = None


class ScorecardRead(ORMBase):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    domain: str | None
    purpose_statement: str | None
    scope: str | None
    target_score: float | None
    status: ScorecardStatus
    current_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class ScorecardVersionCreate(BaseModel):
    version_number: int
    guideline_notes: str | None = None
    is_active: bool = True
    scoring_formula: str | None = None


class ScorecardVersionUpdate(BaseModel):
    guideline_notes: str | None = None
    is_active: bool | None = None
    scoring_formula: str | None = None


class ScorecardVersionRead(ORMBase):
    id: uuid.UUID
    scorecard_id: uuid.UUID
    version_number: int
    guideline_notes: str | None
    created_by: uuid.UUID
    is_active: bool
    scoring_formula: str | None
    created_at: datetime
    updated_at: datetime


class ScorecardVersionReadWithKpiNodes(ScorecardVersionRead):
    kpi_nodes: list[KpiNodeReadWithGuidelines] = []
