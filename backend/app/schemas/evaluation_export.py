from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

MAX_EXPORT_EVALUATIONS = 200


class EvaluationExportRequest(BaseModel):
    """Export the given evaluations as one Excel workbook (see app/reporting)."""

    evaluation_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_EXPORT_EVALUATIONS)
    # False drops the reasoning / evidence / guideline text columns (smaller, shareable file).
    include_reasoning: bool = True
    # Free text shown under the Summary title, e.g. "Workflow: Hackathon · Status: Completed".
    filter_summary: str | None = Field(default=None, max_length=300)

    @field_validator("evaluation_ids")
    @classmethod
    def _dedupe(cls, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(ids))
