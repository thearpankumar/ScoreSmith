from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

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
    jev_raw: dict | None = None
    created_at: datetime
    updated_at: datetime


# Statuses owned by the AI pipeline (dispatcher / workers). A client must never set them directly: a PATCH to
# `queued` / `ingesting` / ... would make a worker pick up (and spend money on) an arbitrary row, or fake a
# lease-less "active" evaluation. Use POST /evaluations/{id}/retry and /cancel instead.
PIPELINE_STATUSES = frozenset(
    {EvaluationStatus.QUEUED, EvaluationStatus.INGESTING, EvaluationStatus.PROCESSING, EvaluationStatus.SCORING}
)


def _client_settable(value: EvaluationStatus | None) -> EvaluationStatus | None:
    if value in PIPELINE_STATUSES:
        raise ValueError("status is managed by the evaluation pipeline; use the retry / cancel endpoints.")
    return value


class EvaluationCreate(BaseModel):
    scorecard_version_id: uuid.UUID
    name: str
    input_reference: dict | None = None
    status: EvaluationStatus = EvaluationStatus.PENDING
    domain: str | None = None

    _check_status = field_validator("status")(_client_settable)


class EvaluationUpdate(BaseModel):
    name: str | None = None
    status: EvaluationStatus | None = None
    final_weighted_score: float | None = Field(default=None, ge=0, le=10)
    rag_band: RagBand | None = None
    submitted_at: datetime | None = None

    _check_status = field_validator("status")(_client_settable)


class EvaluationRead(ORMBase):
    id: uuid.UUID
    scorecard_version_id: uuid.UUID
    name: str
    evaluated_by: uuid.UUID
    runner_name: str | None = None
    input_reference: dict | None
    status: EvaluationStatus
    final_weighted_score: float | None
    rag_band: RagBand | None
    submitted_at: datetime | None
    domain: str | None
    created_at: datetime
    updated_at: datetime
    # AI evaluation pipeline fields (EvaluationSummary in docs/ai-eval-contract.md)
    stage: str | None = None
    subject_name: str | None = None
    subject_email: str | None = None
    batch_id: uuid.UUID | None = None
    source_kind: str | None = None
    direction_prompt: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempt: int = 1
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EvaluationReadWithResults(EvaluationRead):
    kpi_results: list[EvaluationKpiResultRead] = []
