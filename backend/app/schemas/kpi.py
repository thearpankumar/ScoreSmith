from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMBase


class KpiGuidelineCreate(BaseModel):
    score_level: int = Field(ge=0, le=10)
    qualitative_text: str
    quantitative_criteria: dict | None = None


class KpiGuidelineUpdate(BaseModel):
    qualitative_text: str | None = None
    quantitative_criteria: dict | None = None


class KpiGuidelineRead(ORMBase):
    id: uuid.UUID
    kpi_node_id: uuid.UUID
    score_level: int
    qualitative_text: str
    quantitative_criteria: dict | None
    created_at: datetime
    updated_at: datetime


class KpiNodeCreate(BaseModel):
    parent_id: uuid.UUID | None = None
    level: int = Field(ge=1, le=4)
    name: str
    # Nullable: a category/grouping node (one later given children) carries no weight at
    # all — see migration 0008_category_nodes_no_weight. Only leaf KPIs need a real value;
    # the weight-sum-to-100 DB trigger enforces that across every leaf in the scorecard
    # version, not per immediate sibling group.
    weight: float | None = Field(default=None, ge=0, le=100)
    display_order: int = 0
    included_in_scoring: bool = True


class KpiNodeUpdate(BaseModel):
    name: str | None = None
    weight: float | None = Field(default=None, ge=0, le=100)
    display_order: int | None = None
    included_in_scoring: bool | None = None


class KpiNodeRead(ORMBase):
    id: uuid.UUID
    scorecard_version_id: uuid.UUID
    parent_id: uuid.UUID | None
    path: str
    level: int
    name: str
    weight: float | None
    display_order: int
    included_in_scoring: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path_to_str(cls, value: object) -> str:
        # `path` comes off the ORM as a sqlalchemy_utils.Ltree, not a plain str.
        return str(value)


class KpiNodeReadWithGuidelines(KpiNodeRead):
    guidelines: list[KpiGuidelineRead] = []
