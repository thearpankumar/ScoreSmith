from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import EvaluationStatus, RagBand
from app.schemas.common import ORMBase


class EvaluationKpiResultCreate(BaseModel):
    kpi_node_id: uuid.UUID
    score: float = Field(ge=0, le=10)
    matched_guideline_level: int | None = Field(default=None, ge=0, le=10)
    reasoning_text: str | None = None
    evidence_quotes: dict | list | None = None


class EvaluationKpiResultUpdate(BaseModel):
    score: float | None = Field(default=None, ge=0, le=10)
    matched_guideline_level: int | None = Field(default=None, ge=0, le=10)
    reasoning_text: str | None = None
    evidence_quotes: dict | list | None = None


class EvaluationKpiResultRead(ORMBase):
    id: uuid.UUID
    evaluation_id: uuid.UUID
    kpi_node_id: uuid.UUID
    score: float
    matched_guideline_level: int | None
    reasoning_text: str | None
    evidence_quotes: dict | list | None
    # Ensemble judge (k=3 calls/KPI, median aggregation) — see app/ai/judge.py.
    needs_review: bool
    score_variance: float | None
    ensemble_raw_scores: dict | list | None
    created_at: datetime
    updated_at: datetime


class EvaluationCreate(BaseModel):
    scorecard_version_id: uuid.UUID
    name: str
    evaluated_by: uuid.UUID
    input_reference: dict | None = None
    status: EvaluationStatus = EvaluationStatus.PENDING
    domain: str | None = None


class EvaluationUpdate(BaseModel):
    name: str | None = None
    status: EvaluationStatus | None = None
    final_weighted_score: float | None = Field(default=None, ge=0, le=10)
    rag_band: RagBand | None = None
    submitted_at: datetime | None = None


class EvaluationRead(ORMBase):
    id: uuid.UUID
    scorecard_version_id: uuid.UUID
    name: str
    evaluated_by: uuid.UUID
    input_reference: dict | None
    status: EvaluationStatus
    final_weighted_score: float | None
    rag_band: RagBand | None
    submitted_at: datetime | None
    domain: str | None
    created_at: datetime
    updated_at: datetime


class EvaluationReadWithResults(EvaluationRead):
    kpi_results: list[EvaluationKpiResultRead] = []
