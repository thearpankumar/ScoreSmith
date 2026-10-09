"""DB-backed queue + dispatcher for AI evaluations.

- `queued` evaluations are claimed FIFO (`queued_at`) with `SELECT ... FOR UPDATE SKIP LOCKED`, under a
  transaction-scoped advisory lock so the "at most N active" count and the claim are atomic.
  N = `settings.ai_eval_concurrency` (1-5, default 3) evaluations in ingesting/processing/scoring.
- One driver task per claimed evaluation: start the Step Functions execution (name = evaluation id, plus
  `-aN` on retry), poll DescribeExecution + progress.json into `evaluations.progress/stage` and
  `evaluation_events`, and on SUCCEEDED run the local LangGraph scoring (`app.pipeline.graph`).
- Failure / abort / timeout map to the contract error codes (status.json supplies the code on failure).
- Leases (horizontal scaling): a claim stamps `lease_owner` + `lease_expires_at`; a heartbeat renews them
  every `lease_heartbeat_seconds` (~15 s). Several processes (API `all` role or dedicated workers) can run
  dispatchers against one database: each drives only the evaluations it holds a lease on.
- Adoption (`recover()`, also run on every tick): active evaluations whose lease has EXPIRED (owner died, was
  partitioned or shut down cleanly and released it) are RESUMED by whichever process claims them first - by
  `sfn_execution_arn` when they already started an execution (never wiped); scoring is idempotent. A live
  owner's evaluations are never touched, so the spend is never doubled.
- Cancel is a DB flag (`cancel_requested_at`) plus the terminal FAILED/cancelled row: whichever process drives
  the evaluation sees it in `_poll_until_done` / between KPIs (`graph._check_cancel`) and stops. The API process
  need not own the driver.
- Transient scoring failures (model unavailable) are retried up to `ai_eval_scoring_retries` times in `_drive`.
- `settings.ai_eval_inline` (tests): no background loop; drive it with `run_until_idle()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.bedrock_client import BedrockClientProtocol
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.logging_config import bind_log_context
from app.models.enums import ACTIVE_EVALUATION_STATUSES, EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_batch import EvaluationBatch
from app.models.evaluation_source import EvaluationSource
from app.pipeline.aws_jobs import (
    SFN_RUNNING,
    SFN_SUCCEEDED,
    AwsJobsError,
    AwsJobsProtocol,
    AwsNotConfiguredError,
    ExecutionInfo,
    execution_name,
    read_progress,
    read_status,
)
from app.pipeline.events import emit_evaluation_event
from app.pipeline.graph import EvaluationCancelled, PipelineError, ScoringDeps, run_scoring
from app.pipeline.jev_scorer import JevScoreClientProtocol

logger = logging.getLogger(__name__)

ERROR_CODES = frozenset(
    {
        "drive_invalid", "drive_inaccessible", "drive_quota", "drive_empty", "file_too_large",
        "unsupported_type", "extract_failed", "no_content", "timeout", "cancelled", "scoring_failed", "internal",
    }
)
_CLAIM_LOCK_KEY = 7_311_001
_NOT_FINISHED = (EvaluationStatus.QUEUED, *ACTIVE_EVALUATION_STATUSES)
_SOURCE_STATES = {"pending", "running", "done", "skipped", "failed"}


def roll_up_source_states(files: list[dict[str, Any]], source_ids: set[str]) -> dict[str, tuple[str, list[str]]]:
    """Map per-file progress entries onto the evaluation's source rows.

    An uploaded file reports under its own source id. A Drive link expands into several downloaded files
    reported as '{source_id}-NN' (optionally with `parent_source_id`), so the link's row is the roll-up of
    its files: running while any is running, done when any file was processed, failed only when none was.
    Returns {source_id: (status, warnings)}."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        fid = str(f.get("source_id") or "")
        owner = fid if fid in source_ids else str(f.get("parent_source_id") or "")
        if owner not in source_ids and "-" in fid:
            owner = fid.rsplit("-", 1)[0]
        if owner in source_ids:
            groups.setdefault(owner, []).append(f)
    out: dict[str, tuple[str, list[str]]] = {}
    for owner, group in groups.items():
        states = {g.get("state") for g in group if g.get("state") in _SOURCE_STATES}
        if not states:
            continue
        if "running" in states:
            status = "running"
        elif "pending" in states:
            status = "pending"
        elif "done" in states:
            status = "done"
        elif "failed" in states:
            status = "failed"
        else:
            status = "skipped"
        warnings = [
            str(g.get("detail")).strip()
            for g in group
            if g.get("state") in {"skipped", "failed"} and str(g.get("detail") or "").strip()
        ]
        out[owner] = (status, list(dict.fromkeys(warnings)))
    return out


class NotCancellableError(Exception):
    pass


class NotRetryableError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class Dispatcher:
    def __init__(
        self,
        aws: AwsJobsProtocol,
        bedrock: BedrockClientProtocol,
        jev: JevScoreClientProtocol | None,
        *,
        max_concurrent: int | None = None,
        poll_seconds: float | None = None,
        jev_retry_delays: tuple[float, ...] | None = None,
        patience_waits: tuple[float, ...] | None = None,
        score_retry_delays: tuple[float, ...] | None = None,
    ) -> None:
        self.aws = aws
        self.bedrock = bedrock
        self.jev = jev
        # Identifies this dispatcher as a lease holder (unique per instance, so two in one test process differ).
        self.worker_id = f"{platform.node()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._max_concurrent = max_concurrent
        self._poll_seconds = poll_seconds
        self._jev_retry_delays = jev_retry_delays
        self._patience_waits = patience_waits
        self._score_retry_delays = score_retry_delays
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._user_cancelled: set[uuid.UUID] = set()
        self._recovered = False
        self._loop_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None

    # --- settings ---
    @property
    def limit(self) -> int:
        if self._max_concurrent is not None:
            return max(1, min(5, self._max_concurrent))
        return get_settings().ai_eval_concurrency

    @property
    def poll_seconds(self) -> float:
        return self._poll_seconds if self._poll_seconds is not None else get_settings().ai_eval_poll_seconds

    # --- leases ---
    def _lease_values(self) -> dict[str, Any]:
        """Column values that make this dispatcher the lease holder (DB clock: no cross-host skew)."""
        return {
            "lease_owner": self.worker_id,
            "lease_expires_at": func.now() + timedelta(seconds=get_settings().lease_seconds),
            "heartbeat_at": func.now(),
        }

    async def heartbeat(self) -> int:
        """Renews the lease of every evaluation this dispatcher holds. A driver whose evaluation is now
        leased to somebody else (it was adopted while this process was stalled) is cancelled so that the
        evaluation is never driven twice. Returns how many leases were renewed."""
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    update(Evaluation)
                    .where(Evaluation.lease_owner == self.worker_id, Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES))
                    .values(**self._lease_values())
                    .returning(Evaluation.id)
                )
            ).all()
            await db.commit()
            renewed = {r[0] for r in rows}
            missing = [eid for eid, t in self._tasks.items() if eid not in renewed and not t.done()]
            lost: list[uuid.UUID] = []
            if missing:
                held = (
                    await db.execute(
                        select(Evaluation.id, Evaluation.lease_owner).where(
                            Evaluation.id.in_(missing), Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES)
                        )
                    )
                ).all()
                lost = [eid for eid, owner in held if owner != self.worker_id]
        for eid in lost:
            task = self._tasks.get(eid)
            if task is not None and not task.done():
                logger.warning("Lost the lease on evaluation %s; stopping its local driver.", eid)
                task.cancel()
        return len(renewed)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, get_settings().lease_heartbeat_seconds))
            try:
                await self.heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a DB blip must not kill the heartbeat; the lease has slack
                logger.warning("AI evaluation lease heartbeat failed", exc_info=True)

    async def _release_leases(self) -> None:
        """Clean shutdown: hand this process's evaluations back so another worker adopts them at once
        instead of waiting for the lease to expire."""
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Evaluation)
                    .where(Evaluation.lease_owner == self.worker_id, Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES))
                    .values(lease_owner=None, lease_expires_at=None)
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - the lease simply expires instead
            logger.warning("Could not release evaluation leases", exc_info=True)

    async def _adopt_expired(self) -> list[uuid.UUID]:
        """Takes over active evaluations nobody holds a live lease on (never leased, released, or expired)."""
        own = [eid for eid, t in self._tasks.items() if not t.done()]
        async with AsyncSessionLocal() as db:
            cond = Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES) & (
                Evaluation.lease_owner.is_(None) | (Evaluation.lease_expires_at < func.now())
            )
            if own:
                cond = cond & Evaluation.id.not_in(own)
            candidates = select(Evaluation.id).where(cond).with_for_update(skip_locked=True)
            rows = (
                await db.execute(
                    update(Evaluation)
                    .where(Evaluation.id.in_(candidates))
                    .values(**self._lease_values())
                    .returning(Evaluation.id)
                )
            ).all()
            await db.commit()
        return [r[0] for r in rows]

    async def _resume(self, ids: list[uuid.UUID]) -> None:
        for eid in ids:
            await emit_evaluation_event(eid, "resumed", "Resumed by another worker after the previous one stopped.")
            self._spawn(eid)

    # --- lifecycle ---
    async def start(self) -> None:
        """Lifespan hook: recover interrupted evaluations, then run the claim loop in the background."""
        if get_settings().ai_eval_inline or self._loop_task is not None:
            return
        self._wake = asyncio.Event()
        self._loop_task = asyncio.create_task(self._loop(), name="ai-eval-dispatcher")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ai-eval-heartbeat")

    async def stop(self) -> None:
        """Shutdown / SIGTERM drain: stop claiming, stop the drivers WITHOUT failing evaluations, and release
        their leases so another worker resumes them immediately."""
        tasks = [t for t in (self._loop_task, self._heartbeat_task, *self._tasks.values()) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._release_leases()
        self._loop_task = None
        self._heartbeat_task = None
        self._tasks.clear()

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        try:
            await self.recover()
        except Exception:  # noqa: BLE001
            logger.exception("AI evaluation recovery failed")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - DB hiccup must not kill the loop
                logger.exception("AI evaluation dispatcher tick failed")
            assert self._wake is not None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.5, self.poll_seconds))
            self._wake.clear()

    # --- claiming ---
    async def tick(self) -> list[uuid.UUID]:
        """Claims queued evaluations (FIFO) up to the free capacity and starts a driver for each."""
        claimed: list[uuid.UUID] = []
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY})
            active = (
                await db.execute(
                    select(func.count()).select_from(Evaluation).where(Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES))
                )
            ).scalar_one()
            free = self.limit - int(active)
            if free > 0:
                rows = (
                    await db.execute(
                        select(Evaluation)
                        .where(Evaluation.status == EvaluationStatus.QUEUED)
                        .order_by(Evaluation.queued_at.asc().nulls_last(), Evaluation.created_at.asc())
                        .limit(free)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
                now = _now()
                for ev in rows:
                    ev.status = EvaluationStatus.INGESTING
                    ev.started_at = now
                    ev.stage = "ingest"
                    ev.error_code = None
                    ev.error_message = None
                    ev.cancel_requested_at = None
                    for column, value in self._lease_values().items():
                        setattr(ev, column, value)
                    claimed.append(ev.id)
                    if ev.batch_id:
                        await db.execute(
                            update(EvaluationBatch).where(EvaluationBatch.id == ev.batch_id).values(status="running")
                        )
            await db.commit()
        for eid in claimed:
            await emit_evaluation_event(eid, "claimed", "Started processing.")
            self._spawn(eid)
        # Also pick up evaluations whose owner died (lease expired): cheap, and what lets a surviving
        # worker take over without a restart.
        await self._resume(await self._adopt_expired())
        return claimed

    def _spawn(self, eid: uuid.UUID) -> None:
        existing = self._tasks.get(eid)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._drive(eid), name=f"ai-eval-{eid}")
        self._tasks[eid] = task

        def _done(t: asyncio.Task[None]) -> None:
            if self._tasks.get(eid) is t:
                del self._tasks[eid]
            self.wake()

        task.add_done_callback(_done)

    async def recover(self) -> int:
        """Resume active evaluations that nobody holds a live lease on (the previous owner died)."""
        self._recovered = True
        ids = await self._adopt_expired()
        await self._resume(ids)
        return len(ids)

    async def run_until_idle(self, max_iterations: int = 10_000) -> None:
        """Test/debug driver: recover, then claim + run until nothing is queued or active."""
        if not self._recovered:
            await self.recover()
        for _ in range(max_iterations):
            await self.tick()
            tasks = [t for t in self._tasks.values() if not t.done()]
            if not tasks:
                async with AsyncSessionLocal() as db:
                    pending = (
                        await db.execute(
                            select(func.count()).select_from(Evaluation).where(Evaluation.status.in_(_NOT_FINISHED))
                        )
                    ).scalar_one()
                if not pending:
                    return
                await asyncio.sleep(0)
                continue
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)

    # --- the per-evaluation driver ---
    async def _drive(self, eid: uuid.UUID) -> None:
        bind_log_context(evaluation_id=eid)  # every log line of this driver task carries the evaluation id
        try:
            await self._run(eid)
        except asyncio.CancelledError:
            raise  # shutdown or user cancel: the row state is handled elsewhere / resumed on restart
        except EvaluationCancelled:
            await self._cancel_from_flag(eid)
        except PipelineError as exc:
            await self._fail(eid, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI evaluation %s crashed", eid)
            await self._fail(eid, "internal", f"Unexpected error: {exc}")

    async def _load(self, eid: uuid.UUID) -> Evaluation | None:
        async with AsyncSessionLocal() as db:
            return await db.get(Evaluation, eid)

    async def _run(self, eid: uuid.UUID) -> None:
        ev = await self._load(eid)
        if ev is None or ev.status not in ACTIVE_EVALUATION_STATUSES:
            return
        if ev.status != EvaluationStatus.SCORING:
            arn = ev.sfn_execution_arn or await self._start_execution(ev)
            if arn is None:
                return
            if not await self._poll_until_done(eid, arn, ev.started_at):
                return
            moved = await self._to_scoring(eid)
            if not moved:
                return
        await self._score_with_retries(eid)

    async def _start_execution(self, ev: Evaluation) -> str | None:
        settings = get_settings()
        async with AsyncSessionLocal() as db:
            sources = (
                await db.execute(
                    select(EvaluationSource)
                    .where(EvaluationSource.evaluation_id == ev.id)
                    .order_by(EvaluationSource.created_at)
                )
            ).scalars().all()
            payload: dict[str, Any] = {
                "evaluation_id": str(ev.id),
                "bucket": settings.s3_bucket,
                "limits": {"max_file_bytes": settings.upload_max_bytes, "max_files": 20},
                "sources": [
                    {
                        "source_id": str(s.id),
                        "kind": s.kind,
                        "s3_key": s.s3_key,
                        "original_name": s.original_name,
                        "drive_url": s.drive_url,
                    }
                    for s in sources
                ],
            }
        name = execution_name(str(ev.id), ev.attempt)
        try:
            arn = await asyncio.to_thread(self.aws.start_execution, name, payload)
        except AwsNotConfiguredError as exc:
            await self._fail(ev.id, "internal", f"AWS is not configured: {exc}")
            return None
        except AwsJobsError as exc:
            await self._fail(ev.id, "internal", f"Could not start the processing job: {exc}")
            return None
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Evaluation)
                .where(Evaluation.id == ev.id, Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES))
                .values(sfn_execution_arn=arn, stage="ingest")
            )
            await db.commit()
        await emit_evaluation_event(ev.id, "ingest_started", "Fetching and extracting the submission on AWS.")
        return arn

    async def _poll_until_done(self, eid: uuid.UUID, arn: str, started_at: datetime | None) -> bool:
        """True when the execution SUCCEEDED; False when it failed (already recorded) or the evaluation
        was cancelled/removed meanwhile."""
        settings = get_settings()
        deadline = (started_at or _now()) + timedelta(seconds=settings.ai_eval_timeout_seconds)
        last_progress_key: tuple[Any, Any] | None = None
        while True:
            ev = await self._load(eid)
            if ev is None or ev.status not in ACTIVE_EVALUATION_STATUSES:
                return False
            if ev.cancel_requested_at is not None:  # cancel requested through the DB flag (maybe by another process)
                await self._cancel_from_flag(eid)
                return False
            try:
                info: ExecutionInfo = await asyncio.to_thread(self.aws.describe_execution, arn)
                progress = await asyncio.to_thread(read_progress, self.aws, str(eid))
            except AwsJobsError as exc:  # transient: keep polling until the deadline
                logger.warning("poll %s failed: %s", eid, exc)
                info, progress = ExecutionInfo(status=SFN_RUNNING), None
            except AwsNotConfiguredError as exc:
                await self._fail(eid, "internal", f"AWS is not configured: {exc}")
                return False
            if progress:
                last_progress_key = await self._record_progress(eid, progress, last_progress_key)
            if info.status == SFN_SUCCEEDED:
                return True
            if info.status != SFN_RUNNING:
                await self._fail_from_execution(eid, info)
                return False
            if _now() > deadline:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.aws.stop_execution, arn, "timeout")
                await self._fail(eid, "timeout", "The evaluation took too long and was stopped.")
                return False
            await asyncio.sleep(self.poll_seconds)

    async def _record_progress(
        self, eid: uuid.UUID, progress: dict[str, Any], last_key: tuple[Any, Any] | None
    ) -> tuple[Any, Any]:
        stage = str(progress.get("stage") or "")[:80] or None
        message = str(progress.get("message") or "")
        async with AsyncSessionLocal() as db:
            ev = await db.get(Evaluation, eid)
            if ev is None or ev.status not in ACTIVE_EVALUATION_STATUSES:
                return (stage, message)
            ev.progress = progress
            if stage and stage not in {"done", "failed"}:
                ev.stage = stage
            sources = {
                str(s.id): s
                for s in (
                    await db.execute(select(EvaluationSource).where(EvaluationSource.evaluation_id == eid))
                ).scalars().all()
            }
            rolled = roll_up_source_states(progress.get("files") or [], set(sources))
            for source_id, (status, warns) in rolled.items():
                src = sources[source_id]
                src.status = status
                new_warnings = [w for w in warns if w not in (src.warnings or [])]
                if new_warnings:
                    src.warnings = [*(src.warnings or []), *new_warnings]
            await db.commit()
        if (stage, message) != last_key and (stage or message):
            await emit_evaluation_event(eid, f"stage_{stage or 'update'}", message or f"Stage: {stage}")
        return (stage, message)

    async def _fail_from_execution(self, eid: uuid.UUID, info: ExecutionInfo) -> None:
        status_json = await asyncio.to_thread(read_status, self.aws, str(eid))
        code = (status_json or {}).get("error_code")
        message = (status_json or {}).get("error_message")
        if code not in ERROR_CODES:
            code = {"TIMED_OUT": "timeout", "ABORTED": "cancelled"}.get(info.status, "internal")
        if not message:
            message = info.cause or info.error or f"The processing job ended with status {info.status}."
        await self._fail(eid, code, str(message))

    async def _to_scoring(self, eid: uuid.UUID) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Evaluation)
                .where(
                    Evaluation.id == eid,
                    Evaluation.status.in_((EvaluationStatus.INGESTING, EvaluationStatus.PROCESSING)),
                )
                .values(status=EvaluationStatus.SCORING, stage="scoring:start")
                .returning(Evaluation.id)
            )
            moved = result.first() is not None
            await db.commit()
        if moved:
            await emit_evaluation_event(eid, "scoring_queued", "Extraction finished; scoring.")
        return moved

    def _retry_delays(self) -> tuple[float, ...]:
        if self._score_retry_delays is not None:
            return self._score_retry_delays
        settings = get_settings()
        return tuple(settings.ai_eval_scoring_retry_delays)[: max(0, settings.ai_eval_scoring_retries)]

    async def _score_with_retries(self, eid: uuid.UUID) -> None:
        """`_score`, re-run (scoring is idempotent) after a TRANSIENT failure such as the model being
        unavailable, up to `len(delays)` extra times with a jittered wait. Anything else fails at once."""
        delays = self._retry_delays()
        attempt = 0
        while True:
            try:
                await self._score(eid)
                return
            except PipelineError as exc:
                if not exc.transient or attempt >= len(delays):
                    raise
                wait = delays[attempt] * (0.8 + 0.4 * random.random())
                attempt += 1
                logger.warning("Scoring %s failed transiently (%s); retry %d/%d in %.0fs", eid, exc.message, attempt,
                               len(delays), wait)
                await emit_evaluation_event(
                    eid, "scoring_retry",
                    f"Scoring hit a temporary problem ({exc.message}); retrying ({attempt}/{len(delays)}).",
                )
                await asyncio.sleep(wait)
                ev = await self._load(eid)
                if ev is None or ev.status != EvaluationStatus.SCORING:
                    return  # cancelled / removed while waiting

    async def _score(self, eid: uuid.UUID) -> None:
        settings = get_settings()
        extra = {} if self._patience_waits is None else {"patience_waits": self._patience_waits}
        deps = ScoringDeps(self.aws, self.bedrock, self.jev, jev_retry_delays=self._jev_retry_delays, **extra)
        ev = await self._load(eid)
        remaining = settings.ai_eval_timeout_seconds
        if ev is not None and ev.started_at is not None:
            deadline = ev.started_at + timedelta(seconds=settings.ai_eval_timeout_seconds)
            remaining = max(60.0, (deadline - _now()).total_seconds())
        try:
            async with asyncio.timeout(remaining):
                await run_scoring(eid, deps)
        except TimeoutError:
            await self._fail(eid, "timeout", "Scoring took too long and was stopped.")
        finally:
            await self._refresh_batch(eid)

    # --- terminal transitions ---
    async def _fail(self, eid: uuid.UUID, code: str, message: str) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Evaluation)
                .where(Evaluation.id == eid, Evaluation.status.in_(_NOT_FINISHED))
                .values(
                    status=EvaluationStatus.FAILED, error_code=code, error_message=message[:2000],
                    finished_at=_now(), stage="failed", lease_owner=None, lease_expires_at=None,
                )
                .returning(Evaluation.id, Evaluation.batch_id)
            )
            row = result.first()
            if row is not None:
                # Sources that never got processed must not look "pending" on a finished evaluation.
                await db.execute(
                    update(EvaluationSource)
                    .where(EvaluationSource.evaluation_id == eid, EvaluationSource.status.in_(("pending", "running")))
                    .values(status="failed")
                )
                await db.execute(
                    update(EvaluationSource)
                    .where(
                        EvaluationSource.evaluation_id == eid,
                        EvaluationSource.status == "failed",
                        EvaluationSource.warnings.is_(None),
                    )
                    .values(warnings=[message[:300]])
                )
            await db.commit()
        if row is not None:
            await emit_evaluation_event(eid, "failed", f"{code}: {message}")
            await self._refresh_batch(eid)

    async def _refresh_batch(self, eid: uuid.UUID) -> None:
        try:
            async with AsyncSessionLocal() as db:
                ev = await db.get(Evaluation, eid)
                if ev is None or ev.batch_id is None:
                    return
                await refresh_batch_status(db, ev.batch_id)
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("batch status refresh failed", exc_info=True)

    async def _cancel_from_flag(self, eid: uuid.UUID) -> None:
        """A driver saw `cancel_requested_at` on a still-active evaluation: finish the cancellation (the
        terminal FAILED/cancelled row, StopExecution, batch status) exactly as `cancel()` would have."""
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    update(Evaluation)
                    .where(Evaluation.id == eid, Evaluation.status.in_(_NOT_FINISHED))
                    .values(
                        status=EvaluationStatus.FAILED, error_code="cancelled", error_message="Cancelled by the user.",
                        stage="cancelled", finished_at=_now(), lease_owner=None, lease_expires_at=None,
                    )
                    .returning(Evaluation.sfn_execution_arn, Evaluation.batch_id)
                )
            ).first()
            await db.commit()
        if row is None:
            return  # already terminal (cancel() got there first)
        arn, batch_id = row
        await emit_evaluation_event(eid, "cancelled", "Cancelled by the user.")
        if arn:
            try:
                await asyncio.to_thread(self.aws.stop_execution, arn, "Cancelled by the user.")
            except Exception:  # noqa: BLE001 - best effort, the row is already cancelled
                logger.warning("StopExecution failed for %s", eid, exc_info=True)
        if batch_id:
            async with AsyncSessionLocal() as db:
                await refresh_batch_status(db, batch_id)
                await db.commit()

    # --- user actions ---
    async def cancel(self, eid: uuid.UUID) -> None:
        """Cancel a queued/ingesting/scoring evaluation: failed + error_code=cancelled, stop the SFN
        execution (best effort) and the local driver."""
        async with AsyncSessionLocal() as db:
            ev = (
                await db.execute(select(Evaluation).where(Evaluation.id == eid).with_for_update())
            ).scalar_one_or_none()
            if ev is None:
                raise LookupError("Evaluation not found.")
            if ev.status not in _NOT_FINISHED:
                raise NotCancellableError(
                    f"Evaluation is {ev.status.value}; only queued or running ones can be cancelled."
                )
            arn = ev.sfn_execution_arn
            ev.status = EvaluationStatus.FAILED
            ev.error_code = "cancelled"
            ev.error_message = "Cancelled by the user."
            ev.stage = "cancelled"
            ev.finished_at = _now()
            ev.cancel_requested_at = _now()  # drivers on OTHER processes also see the flag
            ev.lease_owner = None
            ev.lease_expires_at = None
            batch_id = ev.batch_id
            await db.commit()
        await emit_evaluation_event(eid, "cancelled", "Cancelled by the user.")
        task = self._tasks.get(eid)
        if task is not None and not task.done():
            self._user_cancelled.add(eid)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._user_cancelled.discard(eid)
        if arn:
            try:
                await asyncio.to_thread(self.aws.stop_execution, arn, "Cancelled by the user.")
            except Exception:  # noqa: BLE001 - the row is already cancelled; AWS stop is best effort
                logger.warning("StopExecution failed for %s", eid, exc_info=True)
        if batch_id:
            async with AsyncSessionLocal() as db:
                await refresh_batch_status(db, batch_id)
                await db.commit()
        self.wake()

    async def retry(self, eid: uuid.UUID) -> None:
        """Re-queue a failed evaluation (new attempt -> new execution name, back of the FIFO queue)."""
        async with AsyncSessionLocal() as db:
            ev = (
                await db.execute(select(Evaluation).where(Evaluation.id == eid).with_for_update())
            ).scalar_one_or_none()
            if ev is None:
                raise LookupError("Evaluation not found.")
            if ev.status != EvaluationStatus.FAILED:
                raise NotRetryableError("Only failed evaluations can be retried.")
            ev.status = EvaluationStatus.QUEUED
            ev.attempt = (ev.attempt or 1) + 1
            ev.error_code = None
            ev.error_message = None
            ev.sfn_execution_arn = None
            ev.progress = None
            ev.stage = "queued"
            ev.queued_at = _now()
            ev.started_at = None
            ev.finished_at = None
            ev.cancel_requested_at = None
            ev.lease_owner = None
            ev.lease_expires_at = None
            await db.execute(
                update(EvaluationSource)
                .where(EvaluationSource.evaluation_id == eid)
                .values(status="pending", warnings=None)
            )
            batch_id = ev.batch_id
            if batch_id:
                await db.execute(update(EvaluationBatch).where(EvaluationBatch.id == batch_id).values(status="running"))
            await db.commit()
        await emit_evaluation_event(eid, "retried", "Re-queued for another attempt.")
        self.wake()


async def refresh_batch_status(db: AsyncSession, batch_id: uuid.UUID) -> None:
    unfinished = (
        await db.execute(
            select(func.count()).select_from(Evaluation).where(
                Evaluation.batch_id == batch_id, Evaluation.status.in_(_NOT_FINISHED)
            )
        )
    ).scalar_one()
    await db.execute(
        update(EvaluationBatch)
        .where(EvaluationBatch.id == batch_id)
        .values(status="running" if unfinished else "completed")
    )


# --- process singleton -----------------------------------------------------------------------------

_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    """FastAPI dependency / lifespan accessor. Overridden in tests via `app.dependency_overrides`."""
    global _dispatcher
    if _dispatcher is None:
        from app import deps

        _dispatcher = Dispatcher(deps.get_aws_jobs(), deps.get_bedrock_client(), deps.get_jev_score_client())
    return _dispatcher


def reset_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None
