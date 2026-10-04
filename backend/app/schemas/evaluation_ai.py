"""Request/response schemas of the AI evaluation endpoints (docs/ai-eval-contract.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase
from app.schemas.evaluation import EvaluationRead

# --- uploads ---


class UploadFileSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int
    content_type: str | None = Field(default=None, max_length=200)


class UploadInitRequest(BaseModel):
    purpose: Literal["submission", "batch_sheet"]
    files: list[UploadFileSpec]


class UploadPart(BaseModel):
    part_number: int
    url: str


class UploadInitFile(BaseModel):
    client_index: int
    upload_id: str
    s3_key: str
    parts: list[UploadPart]


class UploadInitResponse(BaseModel):
    upload_group_id: uuid.UUID
    part_size: int
    files: list[UploadInitFile]


class CompletedPart(BaseModel):
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)


class UploadCompleteFile(BaseModel):
    upload_id: str = Field(min_length=1, max_length=2048)
    s3_key: str = Field(min_length=1, max_length=1024)
    parts: list[CompletedPart] = Field(min_length=1, max_length=10000)


class UploadCompleteRequest(BaseModel):
    files: list[UploadCompleteFile] = Field(min_length=1, max_length=50)


class UploadCompleteResult(BaseModel):
    s3_key: str
    size: int | None
    ok: bool
    error: str | None = None


class UploadCompleteResponse(BaseModel):
    files: list[UploadCompleteResult]


class UploadAbortFile(BaseModel):
    upload_id: str = Field(min_length=1, max_length=2048)
    s3_key: str = Field(min_length=1, max_length=1024)


class UploadAbortRequest(BaseModel):
    files: list[UploadAbortFile] = Field(min_length=1, max_length=50)


# --- batch parse ---


class BatchParseRequest(BaseModel):
    s3_key: str = Field(min_length=1, max_length=1024)


class BatchParsedRow(BaseModel):
    row_index: int
    email: str | None
    name: str | None
    drive_url: str | None
    timestamp: str | None
    warnings: list[str] = []


class BatchSkippedRow(BaseModel):
    row_index: int
    reason: str


class BatchParseResponse(BaseModel):
    rows: list[BatchParsedRow]
    skipped: list[BatchSkippedRow]
    columns: dict[str, str | None]


# --- jobs ---


class JobSource(BaseModel):
    kind: Literal["upload", "drive"]
    s3_key: str | None = Field(default=None, max_length=1024)
    original_name: str | None = Field(default=None, max_length=255)
    size: int | None = Field(default=None, ge=0)
    drive_url: str | None = Field(default=None, max_length=2048)


class JobItem(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    subject_email: str | None = Field(default=None, max_length=320)
    subject_name: str | None = Field(default=None, max_length=255)
    sources: list[JobSource] = Field(min_length=1, max_length=40)


class JobCreateRequest(BaseModel):
    scorecard_id: uuid.UUID
    direction_prompt: str | None = Field(default=None, max_length=2000)
    items: list[JobItem] = Field(min_length=1, max_length=500)


class JobCreateResponse(BaseModel):
    batch_id: uuid.UUID | None
    evaluations: list[EvaluationRead]


# --- progress / batch ---


class SourceRead(ORMBase):
    id: uuid.UUID
    kind: str
    original_name: str | None
    drive_url: str | None
    size: int | None
    status: str
    warnings: list[str] | None = None


class EventRead(ORMBase):
    id: uuid.UUID
    created_at: datetime
    event_type: str
    message: str


class ProgressResponse(BaseModel):
    evaluation_id: uuid.UUID
    status: str
    stage: str | None
    queue_position: int | None
    error_code: str | None
    error_message: str | None
    progress: dict[str, Any] | None
    sources: list[SourceRead]
    events: list[EventRead]


class BatchCounts(BaseModel):
    queued: int
    running: int
    completed: int
    failed: int


class BatchRead(BaseModel):
    id: uuid.UUID
    scorecard_id: uuid.UUID
    status: str
    total: int
    counts: BatchCounts
    evaluations: list[EvaluationRead]
