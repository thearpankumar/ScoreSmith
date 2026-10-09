from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.evaluation_page import EvalFilter

MAX_EXPORT_EVALUATIONS = 200


class EvaluationExportRequest(BaseModel):
    """Export the given evaluations as one Excel workbook (see app/reporting)."""

    # Either explicit ids (max 200), or `filter` = every completed evaluation matching the Evaluations page filters
    # (server-side "select all", max MAX_FILTER_EXPORT) minus `exclude_ids`.
    evaluation_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=MAX_EXPORT_EVALUATIONS)
    filter: EvalFilter | None = None
    exclude_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    # False drops the reasoning / evidence / guideline text columns (smaller, shareable file).
    include_reasoning: bool = True
    # Free text shown under the Summary title, e.g. "Workflow: Hackathon · Status: Completed".
    filter_summary: str | None = Field(default=None, max_length=300)

    @field_validator("evaluation_ids")
    @classmethod
    def _dedupe(cls, ids: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return None if ids is None else list(dict.fromkeys(ids))

    @model_validator(mode="after")
    def _one_of(self) -> EvaluationExportRequest:
        if (self.evaluation_ids is None) == (self.filter is None):
            raise ValueError("Provide either `evaluation_ids` or `filter`.")
        return self
