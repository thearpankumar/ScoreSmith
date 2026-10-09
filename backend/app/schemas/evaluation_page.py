from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import RagBand

StatusGroup = Literal["all", "active", "completed", "failed"]
SortKey = Literal["newest", "oldest", "score_desc", "score_asc", "name_asc", "name_desc"]

MAX_FILTER_EXPORT = 500
MAX_BULK_DELETE = 5000


class EvalFilter(BaseModel):
    """Every filter of the Evaluations page, applied server side (also the body of "all matching" actions)."""

    status: StatusGroup = "all"
    scorecard_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    band: list[RagBand] = Field(default_factory=list)
    min_score: float | None = Field(default=None, ge=0, le=10)
    max_score: float | None = Field(default=None, ge=0, le=10)
    meets_target: bool | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = Field(default=None, max_length=200)
    runner: str | None = Field(default=None, description='"me", "others" or a user id')
    shared: bool | None = Field(default=None, description="True: only charts with collaborators")


class EvalListItem(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    stage: str | None
    final_weighted_score: float | None
    rag_band: str | None
    created_at: datetime
    submitted_at: datetime | None
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    subject_name: str | None
    subject_email: str | None
    batch_id: uuid.UUID | None
    error_code: str | None
    error_message: str | None
    attempt: int
    domain: str | None
    scorecard_id: uuid.UUID
    scorecard_version_id: uuid.UUID
    scorecard_name: str
    target_score: float | None
    runner_id: uuid.UUID
    runner_name: str
    is_mine: bool
    shared: bool


class EvalPage(BaseModel):
    items: list[EvalListItem]
    next_cursor: str | None = None
    # Matching rows (capped at `count_cap`; `total_capped` says the real number is larger), how many of them can be
    # selected / deleted (completed or failed) and how many can be exported (completed with a score).
    total: int
    selectable: int
    exportable: int
    total_capped: bool = False
    count_cap: int


class EvalRefreshRequest(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class BulkSelection(BaseModel):
    """Either explicit ids, or "everything matching `filter` except `exclude_ids`" (server-side select-all)."""

    ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_BULK_DELETE)
    filter: EvalFilter | None = None
    exclude_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_BULK_DELETE)

    @model_validator(mode="after")
    def _one_of(self) -> BulkSelection:
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Provide either `ids` or `filter`.")
        return self


class BulkDeleteResult(BaseModel):
    deleted: int
    skipped: int  # selected rows that were still running or no longer accessible
