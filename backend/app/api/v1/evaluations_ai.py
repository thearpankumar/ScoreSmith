"""AI evaluation pipeline endpoints (docs/ai-eval-contract.md): direct-to-S3 multipart uploads, batch sheet
parsing, job creation (queued evaluations), progress, cancel / retry and batch status.

Registered BEFORE `evaluations.router` (see router.py) so the literal `/evaluations/ai/...` paths can never be
captured by `/evaluations/{evaluation_id}`."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import idempotency as idem
from app.ai.bedrock_client import BedrockClientProtocol
from app.authz import get_accessible_evaluation, get_owned_batch, get_owned_scorecard
from app.config import get_settings
from app.db import get_db
from app.deps import get_aws_jobs, get_bedrock_client, get_current_user
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.evaluation_event import EvaluationEvent
from app.models.evaluation_source import EvaluationSource
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.pipeline import uploads as up
from app.pipeline.aws_jobs import AwsJobsError, AwsJobsProtocol, AwsNotConfiguredError
from app.pipeline.batch_parser import BatchParseError, parse_batch
from app.pipeline.dispatcher import Dispatcher, NotCancellableError, NotRetryableError, get_dispatcher
from app.pipeline.drive import DriveUrlError, classify_drive_url
from app.pipeline.events import emit_evaluation_event
from app.pipeline.graph import PLACEHOLDER_NAME
from app.ratelimit import rate_limit
from app.schemas.evaluation import EvaluationRead
from app.schemas.evaluation_ai import (
    BatchCounts,
    BatchParsedRow,
    BatchParseRequest,
    BatchParseResponse,
    BatchRead,
    BatchSkippedRow,
    EventRead,
    JobCreateRequest,
    JobCreateResponse,
    ProgressResponse,
    SourceRead,
    UploadAbortRequest,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadCompleteResult,
    UploadInitFile,
    UploadInitRequest,
    UploadInitResponse,
    UploadPart,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluations", tags=["evaluations-ai"])

MAX_BATCH_SHEET_BYTES = 20 * 1024 * 1024


def _unprocessable(detail: object) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _aws(fn, *args):
    """Runs a sync AWS call off-loop, mapping failures to HTTP errors."""
    try:
        return await asyncio.to_thread(fn, *args)
    except AwsNotConfiguredError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"File storage is not configured: {exc}"
        ) from exc
    except AwsJobsError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


# --- uploads -----------------------------------------------------------------------------------------


def _initial_name(item: Any) -> str:
    """The name known at submission time: an explicit one, else the sheet's `Name (email)`. Only a
    submission with neither starts as the placeholder that the scoring pipeline replaces later."""
    explicit = (item.name or "").strip()
    if explicit:
        return explicit[:255]
    name = (item.subject_name or "").strip()
    email = (item.subject_email or "").strip().lower()
    if name and email:
        return f"{name} ({email})"[:255]
    return (name or email or PLACEHOLDER_NAME)[:255]


@router.post(
    "/ai/uploads",
    response_model=UploadInitResponse,
    dependencies=[Depends(rate_limit("uploads", lambda s: s.rate_limit_uploads, per_user=True))],
)
async def init_uploads(
    payload: UploadInitRequest,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadInitResponse:
    settings = get_settings()
    max_files = 1 if payload.purpose == "batch_sheet" else settings.upload_max_files
    max_bytes = MAX_BATCH_SHEET_BYTES if payload.purpose == "batch_sheet" else settings.upload_max_bytes
    if not payload.files:
        raise _unprocessable("At least one file is required.")
    if len(payload.files) > max_files:
        raise _unprocessable(f"At most {max_files} file(s) can be uploaded at once.")
    allowed = up.allowed_extensions(payload.purpose)
    errors: list[str] = []
    for i, f in enumerate(payload.files):
        ext = up.file_extension(f.name)
        if ext not in allowed:
            errors.append(f"files[{i}] ({f.name}): unsupported type; allowed: {', '.join(sorted(allowed))}.")
        if f.size <= 0:
            errors.append(f"files[{i}] ({f.name}): file is empty.")
        elif f.size > max_bytes:
            errors.append(f"files[{i}] ({f.name}): larger than the {max_bytes // (1024 * 1024)} MiB limit.")
    if errors:
        raise _unprocessable(errors)
    # Per-user quota: bytes uploaded for new evaluations in the last 24 h plus this request.
    used = (
        await db.execute(
            select(func.coalesce(func.sum(EvaluationSource.size), 0))
            .join(Evaluation, Evaluation.id == EvaluationSource.evaluation_id)
            .where(
                Evaluation.owner_id == current_user.id,
                Evaluation.created_at > datetime.now(UTC) - timedelta(hours=24),
            )
        )
    ).scalar_one()
    if int(used) + sum(f.size for f in payload.files) > settings.upload_user_daily_bytes:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily upload limit reached. Try again later.",
            headers={"Retry-After": "3600"},
        )

    group_id = uuid.uuid4()
    part_size = up.part_size_for(max(f.size for f in payload.files), settings.upload_part_size)
    out: list[UploadInitFile] = []
    started: list[tuple[str, str]] = []
    try:
        for i, f in enumerate(payload.files):
            key = up.build_key(payload.purpose, current_user.id, group_id, up.file_extension(f.name))
            upload_id = await _aws(aws.create_multipart, key, f.content_type)
            started.append((key, upload_id))
            parts = []
            for n in range(1, up.part_count(f.size, part_size) + 1):
                url = await _aws(aws.presign_part, key, upload_id, n, settings.upload_url_ttl_seconds)
                parts.append(UploadPart(part_number=n, url=url))
            out.append(UploadInitFile(client_index=i, upload_id=upload_id, s3_key=key, parts=parts))
    except HTTPException:
        for key, upload_id in started:  # do not leave orphaned multipart uploads behind
            try:
                await asyncio.to_thread(aws.abort_multipart, key, upload_id)
            except Exception:  # noqa: BLE001
                logger.warning("could not abort multipart %s", key)
        raise
    return UploadInitResponse(upload_group_id=group_id, part_size=part_size, files=out)


@router.post("/ai/uploads/complete", response_model=UploadCompleteResponse)
async def complete_uploads(
    payload: UploadCompleteRequest,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    current_user: User = Depends(get_current_user),
) -> UploadCompleteResponse:
    settings = get_settings()
    results: list[UploadCompleteResult] = []
    for f in payload.files:
        ext = up.key_extension(f.s3_key, current_user.id)
        if ext is None:
            results.append(UploadCompleteResult(s3_key=f.s3_key, size=None, ok=False, error="Invalid upload key."))
            continue
        try:
            await asyncio.to_thread(
                aws.complete_multipart, f.s3_key, f.upload_id, [p.model_dump() for p in f.parts]
            )
            head = await asyncio.to_thread(aws.head_object, f.s3_key)
            if head is None:
                results.append(
                    UploadCompleteResult(s3_key=f.s3_key, size=None, ok=False, error="Uploaded file not found.")
                )
                continue
            limit = MAX_BATCH_SHEET_BYTES if ext in up.BATCH_EXTENSIONS else settings.upload_max_bytes
            problem = None
            if head.size <= 0:
                problem = "The uploaded file is empty."
            elif head.size > limit:
                problem = "The uploaded file exceeds the size limit."
            else:
                first = await asyncio.to_thread(aws.read_range, f.s3_key, 0, 16)
                if not up.magic_ok(ext, first):
                    problem = f"The file content does not look like a valid .{ext} file."
            if problem:
                try:
                    await asyncio.to_thread(aws.delete_object, f.s3_key)
                except Exception:  # noqa: BLE001
                    logger.warning("could not delete rejected upload %s", f.s3_key)
                results.append(UploadCompleteResult(s3_key=f.s3_key, size=head.size, ok=False, error=problem))
            else:
                results.append(UploadCompleteResult(s3_key=f.s3_key, size=head.size, ok=True))
        except (AwsJobsError, AwsNotConfiguredError) as exc:
            results.append(UploadCompleteResult(s3_key=f.s3_key, size=None, ok=False, error=str(exc)))
    return UploadCompleteResponse(files=results)


@router.post("/ai/uploads/abort", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def abort_uploads(
    payload: UploadAbortRequest,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    current_user: User = Depends(get_current_user),
) -> Response:
    for f in payload.files:
        if up.key_extension(f.s3_key, current_user.id) is None:
            continue  # not a key we issued to this user: ignore silently
        try:
            await asyncio.to_thread(aws.abort_multipart, f.s3_key, f.upload_id)
        except (AwsJobsError, AwsNotConfiguredError):
            logger.warning("abort_multipart failed for %s", f.s3_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- batch parse -------------------------------------------------------------------------------------


@router.post("/ai/batches/parse", response_model=BatchParseResponse)
async def parse_batch_sheet(
    payload: BatchParseRequest,
    aws: AwsJobsProtocol = Depends(get_aws_jobs),
    bedrock: BedrockClientProtocol = Depends(get_bedrock_client),
    current_user: User = Depends(get_current_user),
) -> BatchParseResponse:
    ext = up.key_extension(payload.s3_key, current_user.id, "batch_sheet")
    if ext is None:
        raise _unprocessable("s3_key is not an uploaded batch sheet (.xlsx / .csv).")
    data = await _aws(aws.get_object_bytes, payload.s3_key, MAX_BATCH_SHEET_BYTES)
    try:
        preview = await parse_batch(bedrock, get_settings().bedrock_master_model_id, f"sheet.{ext}", data)
    except BatchParseError as exc:
        raise _unprocessable(str(exc)) from exc
    return BatchParseResponse(
        rows=[
            BatchParsedRow(
                row_index=r.row_index, email=r.email, name=r.name, drive_url=r.drive_url,
                timestamp=r.timestamp, warnings=r.warnings,
            )
            for r in preview.rows
        ],
        skipped=[BatchSkippedRow(**s) for s in preview.skipped],
        columns=preview.columns,
    )


# --- jobs --------------------------------------------------------------------------------------------


async def _resolve_version(db: AsyncSession, scorecard: Scorecard) -> ScorecardVersion:
    version = await db.get(ScorecardVersion, scorecard.current_version_id) if scorecard.current_version_id else None
    if version is None:
        version = (
            await db.execute(
                select(ScorecardVersion)
                .where(ScorecardVersion.scorecard_id == scorecard.id)
                .order_by(ScorecardVersion.is_active.desc(), ScorecardVersion.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if version is None:
        raise _unprocessable("The scorecard has no version to evaluate against.")
    has_kpis = (
        await db.execute(select(func.count()).select_from(KpiNode).where(KpiNode.scorecard_version_id == version.id))
    ).scalar_one()
    if not has_kpis:
        raise _unprocessable("The scorecard has no KPIs to evaluate against.")
    return version


@router.post(
    "/ai/jobs",
    response_model=JobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("ai_jobs", lambda s: s.rate_limit_ai_jobs, per_user=True))],
)
async def create_jobs(
    payload: JobCreateRequest,
    db: AsyncSession = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JobCreateResponse:
    settings = get_settings()
    # `Idempotency-Key`: a retried POST (timeout, double click) returns the evaluations / batch created the
    # first time instead of queueing - and paying for - them again.
    key = idem.normalize_key(idempotency_key)
    if key:
        stored = await idem.begin(
            db, current_user.id, "ai_jobs", key, idem.request_fingerprint(payload.model_dump_json())
        )
        if stored is not None:
            ids = [uuid.UUID(x) for x in stored["evaluation_ids"]]
            existing = (
                await db.execute(
                    select(Evaluation)
                    .where(Evaluation.id.in_(ids))
                    .order_by(Evaluation.queued_at, Evaluation.created_at)
                )
            ).scalars().all()
            return JobCreateResponse(
                batch_id=uuid.UUID(stored["batch_id"]) if stored.get("batch_id") else None,
                evaluations=[EvaluationRead.model_validate(e) for e in existing],
            )
    scorecard = await get_owned_scorecard(db, current_user, payload.scorecard_id)

    errors: list[dict] = []
    for i, item in enumerate(payload.items):
        n_uploads = 0
        for j, src in enumerate(item.sources):
            where = {"item": i, "source": j}
            if src.kind == "upload":
                n_uploads += 1
                if not src.s3_key or up.key_extension(src.s3_key, current_user.id, "submission") is None:
                    errors.append({**where, "error": "Invalid or foreign upload key."})
            else:
                try:
                    classify_drive_url(src.drive_url or "")
                except DriveUrlError as exc:
                    errors.append({**where, "error": f"Invalid Drive link: {exc}"})
        if n_uploads > settings.upload_max_files:
            errors.append({"item": i, "error": f"At most {settings.upload_max_files} uploaded files per submission."})
    if errors:
        raise _unprocessable(errors)

    version = await _resolve_version(db, scorecard)
    direction = (payload.direction_prompt or "").strip() or None

    batch: EvaluationBatch | None = None
    if len(payload.items) > 1:
        batch = EvaluationBatch(
            scorecard_id=scorecard.id, created_by=current_user.id, row_count=len(payload.items), status="queued"
        )
        db.add(batch)
        await db.flush()

    base = datetime.now(UTC)
    evaluations: list[Evaluation] = []
    for i, item in enumerate(payload.items):
        kinds = {s.kind for s in item.sources}
        ev = Evaluation(
            scorecard_version_id=version.id,
            name=_initial_name(item),
            evaluated_by=current_user.id,
            owner_id=current_user.id,
            input_reference={"origin": "ai_pipeline"},
            status=EvaluationStatus.QUEUED,
            domain=scorecard.domain,
            batch_id=batch.id if batch else None,
            source_kind=kinds.pop() if len(kinds) == 1 else "mixed",
            direction_prompt=direction,
            subject_name=(item.subject_name or "").strip() or None,
            subject_email=(item.subject_email or "").strip().lower() or None,
            stage="queued",
            queued_at=base + timedelta(milliseconds=i),
            attempt=1,
        )
        db.add(ev)
        await db.flush()
        for src in item.sources:
            db.add(
                EvaluationSource(
                    evaluation_id=ev.id,
                    kind=src.kind,
                    s3_key=src.s3_key if src.kind == "upload" else None,
                    original_name=src.original_name,
                    drive_url=src.drive_url if src.kind == "drive" else None,
                    size=src.size,
                    status="pending",
                )
            )
        evaluations.append(ev)
    if key:
        await idem.remember(
            db, current_user.id, "ai_jobs", key,
            {"batch_id": str(batch.id) if batch else None, "evaluation_ids": [str(e.id) for e in evaluations]},
        )
    await db.commit()
    for ev in evaluations:
        await db.refresh(ev)
        await emit_evaluation_event(ev.id, "queued", "Queued for processing.")
    dispatcher.wake()
    return JobCreateResponse(
        batch_id=batch.id if batch else None,
        evaluations=[EvaluationRead.model_validate(e) for e in evaluations],
    )


# --- batch status ------------------------------------------------------------------------------------


@router.get("/ai/batches/{batch_id}", response_model=BatchRead)
async def get_batch(
    batch_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> BatchRead:
    batch = await get_owned_batch(db, current_user, batch_id)
    evaluations = list(
        (
            await db.execute(
                select(Evaluation)
                .where(Evaluation.batch_id == batch_id)
                .order_by(Evaluation.queued_at, Evaluation.created_at)
            )
        ).scalars().all()
    )
    counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
    for e in evaluations:
        if e.status in (EvaluationStatus.QUEUED, EvaluationStatus.PENDING):
            counts["queued"] += 1
        elif e.status == EvaluationStatus.COMPLETED:
            counts["completed"] += 1
        elif e.status == EvaluationStatus.FAILED:
            counts["failed"] += 1
        else:
            counts["running"] += 1
    done = counts["queued"] + counts["running"] == 0
    started = bool(counts["running"] or counts["completed"] or counts["failed"])
    return BatchRead(
        id=batch.id,
        scorecard_id=batch.scorecard_id,
        status="completed" if done else ("running" if started else "queued"),
        total=len(evaluations),
        counts=BatchCounts(**counts),
        evaluations=[EvaluationRead.model_validate(e) for e in evaluations],
    )


# --- per-evaluation progress / cancel / retry --------------------------------------------------------


@router.get("/{evaluation_id}/progress", response_model=ProgressResponse)
async def get_progress(
    evaluation_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> ProgressResponse:
    ev = await get_accessible_evaluation(db, current_user, evaluation_id)
    position: int | None = None
    if ev.status == EvaluationStatus.QUEUED:
        ahead = (
            await db.execute(
                select(func.count()).select_from(Evaluation).where(
                    Evaluation.status == EvaluationStatus.QUEUED,
                    Evaluation.id != ev.id,
                    (Evaluation.queued_at < ev.queued_at)
                    | ((Evaluation.queued_at == ev.queued_at) & (Evaluation.created_at < ev.created_at)),
                )
            )
        ).scalar_one()
        position = int(ahead) + 1
    sources = (
        await db.execute(
            select(EvaluationSource)
            .where(EvaluationSource.evaluation_id == ev.id)
            .order_by(EvaluationSource.created_at)
        )
    ).scalars().all()
    events = (
        await db.execute(
            select(EvaluationEvent)
            .where(EvaluationEvent.evaluation_id == ev.id)
            .order_by(EvaluationEvent.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return ProgressResponse(
        evaluation_id=ev.id,
        status=ev.status.value,
        stage=ev.stage,
        queue_position=position,
        error_code=ev.error_code,
        error_message=ev.error_message,
        progress=ev.progress,
        sources=[SourceRead.model_validate(s) for s in sources],
        events=[EventRead.model_validate(e) for e in reversed(events)],
    )


@router.post("/{evaluation_id}/cancel", response_model=EvaluationRead, status_code=status.HTTP_202_ACCEPTED)
async def cancel_evaluation(
    evaluation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    current_user: User = Depends(get_current_user),
) -> Evaluation:
    await get_accessible_evaluation(db, current_user, evaluation_id)
    try:
        await dispatcher.cancel(evaluation_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Evaluation not found.") from exc
    except NotCancellableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.expire_all()
    return await db.get(Evaluation, evaluation_id)


@router.post("/{evaluation_id}/retry", response_model=EvaluationRead, status_code=status.HTTP_202_ACCEPTED)
async def retry_evaluation(
    evaluation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    current_user: User = Depends(get_current_user),
) -> Evaluation:
    await get_accessible_evaluation(db, current_user, evaluation_id)
    try:
        await dispatcher.retry(evaluation_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Evaluation not found.") from exc
    except NotRetryableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.expire_all()
    return await db.get(Evaluation, evaluation_id)

