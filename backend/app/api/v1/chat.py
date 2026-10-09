"""Chat-driven scorecard builder endpoints, backed by the LangGraph state machine in
`app/ai/scorecard_builder.py`.

`chat_sessions.id` is used directly as the LangGraph `thread_id` (see
`scorecard_builder.py` module docstring for the rationale). Every turn is also written to
`chat_messages` as plain relational history — independent of, and in addition to,
LangGraph's own checkpoint tables — so the existing chat data model stays populated for
any consumer that doesn't know about LangGraph.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import notifications as notif
from app.activity import log_edit
from app.ai.bedrock_client import BedrockClientProtocol, BedrockUnavailableError
from app.ai.draft_materialize import draft_from_scorecard, materialize_draft
from app.ai.draft_schema import ScorecardDraft
from app.ai.embeddings import upsert_scorecard_embedding
from app.ai.jev_client import JevClientProtocol
from app.ai.scorecard_builder import (
    BuilderTurnResult,
    delete_session_checkpoints,
    get_session_state,
    seed_session,
    send_message,
    start_session,
)
from app.ai.session_title import generate_session_title
from app.ai.web_search import WebSearchClientProtocol
from app.authz import get_accessible_scorecard, get_owned_chat_session
from app.config import get_settings
from app.db import AsyncSessionLocal, get_db
from app.deps import get_bedrock_client, get_current_user, get_jev_client, get_web_search_client
from app.logging_config import bind_log_context
from app.models.chat_message import ChatMessage
from app.models.chat_session import STALE_TURN_TIMEOUT_SECONDS, ChatSession
from app.models.chat_turn_event import ChatTurnEvent
from app.models.enums import ChatMessageRole, ChatSessionStatus
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.ratelimit import rate_limit
from app.schemas.chat import (
    ChatMessageCreate,
    ChatMessageRead,
    ChatSessionRead,
    ChatSessionStart,
    ChatTurnEventRead,
    ChatTurnRead,
)
from app.slots import acquire_chat_slot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# --- Persistent "turn in progress" marker (Part A: refresh-survives-mid-turn) -----------
#
# Chat turns run either INLINE in the request (`?wait=true`, or `settings.chat_turns_inline` —
# the original behaviour, used by the test suite) or, by default, as a BACKGROUND task after a
# 202 response (see "Background turns" below). `ChatSession.pending_turn_started_at`
# (and its `turn_in_progress` computed property, which applies the staleness cutoff — see
# `STALE_TURN_TIMEOUT_SECONDS` on that model) is the durable record a page refresh checks
# via `GET /chat/sessions/{id}` to know "the assistant is still working on this" instead of
# rendering a blank composer as if nothing were happening. These two helpers use a plain
# `UPDATE ... WHERE id = :id` (not the possibly-stale ORM object already in `db`'s
# identity map) and commit immediately, so the write is visible to a *different* request's
# connection (a concurrent GET from the refreshed tab) as soon as it lands — no need to
# keep this request's own transaction open across the long Bedrock/LangGraph call.


# Clearing the in-progress marker also clears the queued job + lease + cancel flag (migration 0011).
_TURN_RESET: dict = {
    "turn_message": None,
    "turn_first": None,
    "turn_lease_owner": None,
    "turn_lease_expires_at": None,
    "turn_heartbeat_at": None,
    "turn_cancel_requested_at": None,
}


async def _mark_turn_in_progress(
    db: AsyncSession, session_id: uuid.UUID, *, require_idle: bool = False
) -> datetime:
    """Returns the exact timestamp written, so the caller can hand it to
    `start_session`/`send_message` as `turn_started_at` — this is the value every
    `chat_turn_events` row this turn's graph nodes write gets tagged with (see
    `app/ai/turn_events.py`), correlating the live-trace log to this specific turn attempt
    the same way `pending_turn_started_at` already correlates the boolean marker.

    Also clears out any `chat_turn_events` left over from this session's PREVIOUS turn
    before starting the new one — bounds the table to roughly one turn's worth of rows per
    active session (see `GET /chat/sessions/{id}/turn-events`'s own docstring for the read
    side of this "don't accumulate unboundedly" contract) rather than growing forever
    across a long-lived session's history of turns."""
    started_at = datetime.now(UTC)
    # One running turn per USER across all their sessions (app/slots.py): atomic with the claim below.
    owner_id = await db.scalar(select(ChatSession.user_id).where(ChatSession.id == session_id))
    if owner_id is not None:
        await acquire_chat_slot(db, owner_id, session_id)
    conditions = [ChatSession.id == session_id]
    if require_idle:
        # Atomic "no turn running" check (two concurrent POSTs must not both start a turn on one thread): a
        # marker older than the stale timeout is a crashed turn and may be taken over.
        stale_before = started_at - timedelta(seconds=STALE_TURN_TIMEOUT_SECONDS)
        conditions.append(
            ChatSession.pending_turn_started_at.is_(None) | (ChatSession.pending_turn_started_at < stale_before)
        )
    claimed = await db.execute(
        update(ChatSession)
        .where(*conditions)
        .values(pending_turn_started_at=started_at, last_turn_error=None, last_turn_error_code=None, **_TURN_RESET)
        .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
        .returning(ChatSession.id)
    )
    if claimed.first() is None and require_idle:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="The assistant is still working on your previous message."
        )
    await db.execute(delete(ChatTurnEvent).where(ChatTurnEvent.session_id == session_id))
    await db.commit()
    return started_at


async def _mark_new_turn(db: AsyncSession, session_id: uuid.UUID) -> datetime:
    """`_mark_turn_in_progress` for a session created a moment ago: when the user's chat slot is busy the fresh
    (empty) session is removed again, so a refused first message leaves nothing behind."""
    try:
        return await _mark_turn_in_progress(db, session_id)
    except HTTPException:
        await db.rollback()
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
        await db.commit()
        raise


async def _clear_turn_in_progress(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(ChatSession).where(ChatSession.id == session_id).values(pending_turn_started_at=None, **_TURN_RESET)
    )
    await db.commit()


async def _generate_and_persist_title(
    db: AsyncSession, session: ChatSession, bedrock: BedrockClientProtocol, first_message: str
) -> str | None:
    """Issue 1 (see task notes): a brand-new session's title must be generated as the
    VERY FIRST thing on its first turn — before check_similarity/research_kpis/
    propose_kpis (the heavier LangGraph work — see scorecard_builder.py) ever run — via
    ONE fast/cheap Bedrock call (settings.bedrock_judge_model_id, not the heavier chat
    model), and persisted immediately so a concurrent request (a different tab's session
    list poll, or this same request's own eventual response) can see it well before the
    graph call returns. Best-effort: never raises, never blocks session creation on a
    cosmetic feature — see generate_session_title's own "never raise" contract."""
    settings = get_settings()
    title = await asyncio.to_thread(
        generate_session_title, bedrock, settings.bedrock_judge_model_id, first_message
    )
    if not title:
        return None
    await db.execute(update(ChatSession).where(ChatSession.id == session.id).values(title=title))
    await db.commit()
    session.title = title
    logger.info("Generated chat session title for session_id=%s: %r", session.id, title)
    return title


# --- Background turns --------------------------------------------------------------------
#
# Why: a turn can run for minutes (research fan-out, enrichment, 30+ KPI requests), which as ONE
# synchronous HTTP request is a hang/timeout risk (browser, proxy and server limits) and ties the
# whole turn to a connection that may drop. Design (the long-running-operation pattern: accept,
# return 202, poll a status resource):
#   - POST marks the turn in progress (`pending_turn_started_at`, durable), stores the job (`turn_message`,
#     `turn_first`) on the session row and returns 202 immediately. The row IS the queue entry.
#   - A turn runner (`ChatTurnRunner`) claims it with `FOR UPDATE SKIP LOCKED` and runs the same LangGraph
#     turn (`start_session`/`send_message`) in an `asyncio.Task` that opens its OWN `AsyncSession`. With
#     `ROLE=all` (dev, tests) the API process claims its own turn on the spot - with the request's injected
#     clients - so there is no queue latency; with `ROLE=api` the API only enqueues and a `ROLE=worker`
#     process (app/worker.py) claims it. Any number of workers can run side by side.
#   - Lease: the claim stamps `turn_lease_owner` / `turn_lease_expires_at`, renewed ~every 15 s by the runner's
#     supervisor. A lease that EXPIRES means the worker died: the turn is recorded as "interrupted" (the user
#     is offered a retry) - a live worker's turns are never touched, which is what the old "clear every marker
#     at boot" recovery could not guarantee with more than one process.
#   - Cancel / delete is a DB flag (`turn_cancel_requested_at`), because the API process serving the request is
#     usually not the one running the turn. The runner's supervisor (every `chat_supervisor_seconds`) cancels the
#     task when it sees the flag, or when the session row is gone. A turn running in THIS process is cancelled
#     directly as a fast path.
#   - Live progress is the existing `chat_turn_events` rows; the client polls `GET /chat/sessions/{id}` (state +
#     `turn_in_progress` + `turn_error`) and `GET .../turn-events`. Polling over SSE: all state is already
#     durable in Postgres, so a refresh/another tab/a server restart just resumes polling.
#   - Failure is recorded in `chat_sessions.last_turn_error[_code]` (migration 0009) and the marker cleared. A hard
#     wall-clock limit (`settings.chat_turn_timeout_seconds`) bounds the task. SIGTERM / shutdown drains: stop
#     claiming, cancel running tasks (each records "interrupted").
#   - Windows: the task runs on the SAME event loop as the server (set to the selector policy in
#     app/main.py before the loop exists); no thread/loop is created here.

# session id -> its running background turn IN THIS PROCESS, so DELETE / cancel can stop it directly and
# shutdown can drain it. (asyncio only holds weak references to tasks: this dict also keeps them alive.)
_turn_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
_user_cancelled: set[uuid.UUID] = set()


def _use_inline_turns(wait: bool | None) -> bool:
    return get_settings().chat_turns_inline if wait is None else wait


async def _record_turn_failure(session_id: uuid.UUID, turn_started_at: datetime, code: str, message: str) -> None:
    """Clears the in-progress marker of THIS turn and stores why it failed (fresh session — the
    caller's may be broken). Best-effort: never raises."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ChatSession)
                .where(ChatSession.id == session_id, ChatSession.pending_turn_started_at == turn_started_at)
                .values(
                    pending_turn_started_at=None, last_turn_error=message, last_turn_error_code=code, **_TURN_RESET
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning("Could not record turn failure for session_id=%s.", session_id, exc_info=True)


async def _persist_turn_result(
    db: AsyncSession,
    session: ChatSession,
    turn: BuilderTurnResult,
    bedrock: BedrockClientProtocol,
    *,
    user_message: str | None,
) -> None:
    """Writes the turn's chat_messages rows (the user's message too when `user_message` is given —
    first turns persist it only after success), materializes a confirmed draft and clears the
    in-progress marker."""
    if user_message is not None:
        await _persist_message(db, session.id, ChatMessageRole.USER, user_message)
    tool_calls = None
    if turn.question:
        tool_calls = {"question": turn.question}
    elif turn.similar_suggestions:
        tool_calls = {"similar_suggestions": turn.similar_suggestions}
    await _persist_message(db, session.id, ChatMessageRole.ASSISTANT, turn.assistant_note or "", tool_calls=tool_calls)
    await _maybe_materialize(db, session, turn, bedrock)
    await _clear_turn_in_progress(db, session.id)
    await db.commit()


async def _notify_turn(session: ChatSession, turn: BuilderTurnResult, turn_started_at: datetime) -> None:
    """Background turns only (the user may be away): "the assistant asked you something" and "your scorecard was
    saved". Idempotent per turn (dedupe key), so a retried / re-driven turn never notifies twice."""
    stamp = turn_started_at.isoformat()
    title = (session.title or "your chat")[:120]
    if turn.question:
        await notif.notify_background(
            session.user_id, notif.CHAT_QUESTION, "The assistant has a question for you",
            body=f"In '{title}': {str(turn.question)[:240]}", link=f"/chat/{session.id}",
            data={"session_id": str(session.id)}, dedupe_key=f"question:{session.id}:{stamp}",
        )
    if turn.status == "confirmed" and session.target_scorecard_id is not None:
        await notif.notify_background(
            session.user_id, notif.SCORECARD_SAVED, "Your scorecard was saved",
            body=f"'{title}' finished building.", link=f"/charts/{session.target_scorecard_id}",
            data={"session_id": str(session.id), "scorecard_id": str(session.target_scorecard_id)},
            dedupe_key=f"saved:{session.id}:{stamp}",
        )


async def _background_turn(
    *,
    session_id: uuid.UUID,
    message: str,
    first_turn: bool,
    persist_user_message: bool,
    turn_started_at: datetime,
    bedrock: BedrockClientProtocol,
    web_search: WebSearchClientProtocol,
    jev: JevClientProtocol,
) -> None:
    """The body of one background turn — see the "Background turns" comment above. Never raises
    (except re-raising cancellation after recording it)."""
    settings = get_settings()
    bind_log_context(session_id=session_id)  # every log line of this turn carries the session id
    try:
        async with AsyncSessionLocal() as db:
            session = await db.get(ChatSession, session_id)
            if session is None:
                return  # deleted before the task even started
            if first_turn:
                await _generate_and_persist_title(db, session, bedrock, message)
            # Never carry an idle-in-transaction connection into the (minutes-long) graph run:
            # it would block any schema DDL (e.g. the checkpointer's CREATE INDEX CONCURRENTLY).
            await db.commit()
            async with asyncio.timeout(settings.chat_turn_timeout_seconds):
                run = start_session if first_turn else send_message
                turn = await run(
                    str(session_id),
                    message,
                    bedrock,
                    chat_model_id=settings.bedrock_chat_model_id,
                    db=db,
                    web_search_client=web_search,
                    turn_started_at=turn_started_at,
                    jev_client=jev,
                )
            await _persist_turn_result(
                db, session, turn, bedrock, user_message=message if persist_user_message else None
            )
            await _notify_turn(session, turn, turn_started_at)
    except asyncio.CancelledError:
        if session_id in _user_cancelled:
            _user_cancelled.discard(session_id)
            await asyncio.shield(
                _record_turn_failure(session_id, turn_started_at, "cancelled", "This turn was cancelled.")
            )
        else:
            await asyncio.shield(
                _record_turn_failure(
                    session_id, turn_started_at, "interrupted", "The server restarted while working on your message."
                )
            )
        raise
    except BedrockUnavailableError as exc:
        await _record_turn_failure(session_id, turn_started_at, "bedrock_unavailable", str(exc))
    except TimeoutError:
        await _record_turn_failure(
            session_id, turn_started_at, "timeout",
            f"The assistant took longer than {settings.chat_turn_timeout_seconds}s and was stopped.",
        )
    except IntegrityError:
        logger.info("Chat session %s was deleted while its turn ran; dropping the result.", session_id)
    except Exception:  # noqa: BLE001 — a background task has no caller to raise to
        logger.exception("Background chat turn failed (session_id=%s).", session_id)
        await _record_turn_failure(
            session_id, turn_started_at, "turn_failed", "Something went wrong while processing your message."
        )


def _chat_lease_values(worker_id: str) -> dict:
    return {
        "turn_lease_owner": worker_id,
        "turn_lease_expires_at": func.now() + timedelta(seconds=get_settings().lease_seconds),
        "turn_heartbeat_at": func.now(),
    }


class ChatTurnRunner:
    """Claims queued background chat turns, runs them, keeps their leases alive and watches for cancels.

    One per process (`get_turn_runner()`); started by the `all` / `worker` roles. See "Background turns"."""

    def __init__(self) -> None:
        self.worker_id = f"{platform.node()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._claiming = True
        self._stopping = False  # set by stop(): the supervisor loop must end even if its cancellation is swallowed
        self._supervisor: asyncio.Task[None] | None = None
        self._last_beat = float("-inf")
        self._last_reap = float("-inf")

    # --- enqueue / spawn ---
    async def enqueue(
        self, session_id: uuid.UUID, message: str, first_turn: bool, turn_started_at: datetime, *, claim: bool
    ) -> None:
        """Stores the job on the session row. `claim=True` also takes the lease for THIS process (the caller
        is about to run the turn itself); `claim=False` leaves it for any worker to claim."""
        values: dict = {"turn_message": message, "turn_first": first_turn, "turn_cancel_requested_at": None}
        values.update(
            _chat_lease_values(self.worker_id)
            if claim
            else {"turn_lease_owner": None, "turn_lease_expires_at": None, "turn_heartbeat_at": None}
        )
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ChatSession)
                .where(ChatSession.id == session_id, ChatSession.pending_turn_started_at == turn_started_at)
                .values(**values)
            )
            await db.commit()

    def spawn(self, session_id: uuid.UUID, coro) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=f"chat-turn-{session_id}")
        _turn_tasks[session_id] = task

        def _done(t: asyncio.Task[None]) -> None:
            if _turn_tasks.get(session_id) is t:
                del _turn_tasks[session_id]

        task.add_done_callback(_done)
        return task

    def running(self) -> int:
        return sum(1 for t in _turn_tasks.values() if not t.done())

    # --- claiming ---
    async def claim(self) -> int:
        """Claims queued turns nobody holds a lease on (FIFO, SKIP LOCKED) up to the free capacity."""
        free = get_settings().chat_turn_max_concurrent - self.running()
        if free <= 0 or not self._claiming:
            return 0
        async with AsyncSessionLocal() as db:
            candidates = (
                select(ChatSession.id)
                .where(
                    ChatSession.pending_turn_started_at.is_not(None),
                    ChatSession.turn_message.is_not(None),
                    ChatSession.turn_lease_owner.is_(None),
                    ChatSession.turn_cancel_requested_at.is_(None),
                )
                .order_by(ChatSession.pending_turn_started_at)
                .limit(free)
                .with_for_update(skip_locked=True)
            )
            rows = (
                await db.execute(
                    update(ChatSession)
                    .where(ChatSession.id.in_(candidates))
                    .values(**_chat_lease_values(self.worker_id))
                    .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
                    .returning(
                        ChatSession.id, ChatSession.turn_message, ChatSession.turn_first,
                        ChatSession.pending_turn_started_at,
                    )
                )
            ).all()
            await db.commit()
        if rows:
            from app import deps  # a worker has no request to inject clients: use the process singletons

            bedrock, web_search, jev = deps.get_bedrock_client(), deps.get_web_search_client(), deps.get_jev_client()
        for session_id, message, first_turn, started_at in rows:
            logger.info("Claimed chat turn for session_id=%s", session_id)
            self.spawn(
                session_id,
                _background_turn(
                    session_id=session_id, message=message, first_turn=bool(first_turn), persist_user_message=False,
                    turn_started_at=started_at, bedrock=bedrock, web_search=web_search, jev=jev,
                ),
            )
        return len(rows)

    async def reap(self) -> int:
        """Turns whose lease EXPIRED belong to a worker that died: record them as interrupted so the UI
        offers a retry. A live worker keeps renewing its lease, so its turns are never reaped."""
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    update(ChatSession)
                    .where(
                        ChatSession.pending_turn_started_at.is_not(None),
                        ChatSession.turn_lease_owner.is_not(None),
                        ChatSession.turn_lease_expires_at < func.now(),
                    )
                    .values(
                        pending_turn_started_at=None,
                        last_turn_error="The server restarted while working on your message. Please send it again.",
                        last_turn_error_code="interrupted",
                        **_TURN_RESET,
                    )
                )
                await db.commit()
                return result.rowcount or 0
        except Exception:  # noqa: BLE001 — e.g. DB not reachable/migrated yet; never block startup
            logger.warning("Could not reap expired chat-turn leases.", exc_info=True)
            return 0

    # --- supervision: heartbeat + cross-process cancel ---
    async def supervise_once(self) -> None:
        """Renews the leases of this process's turns (every `lease_heartbeat_seconds`) and applies cancel
        requests. A turn whose row no longer shows this process as leaseholder - session deleted, or lease
        lost after a stall - is cancelled so it cannot outlive its record."""
        ids = [sid for sid, t in _turn_tasks.items() if not t.done()]
        if not ids:
            return
        settings = get_settings()
        renew = time.monotonic() - self._last_beat >= settings.lease_heartbeat_seconds
        held = (
            ChatSession.id.in_(ids),
            ChatSession.turn_lease_owner == self.worker_id,
            ChatSession.pending_turn_started_at.is_not(None),
        )
        async with AsyncSessionLocal() as db:
            if renew:
                rows = (
                    await db.execute(
                        update(ChatSession)
                        .where(*held)
                        .values(**_chat_lease_values(self.worker_id))
                        .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
                        .returning(ChatSession.id, ChatSession.turn_cancel_requested_at)
                    )
                ).all()
                await db.commit()
                self._last_beat = time.monotonic()
            else:
                rows = (
                    await db.execute(select(ChatSession.id, ChatSession.turn_cancel_requested_at).where(*held))
                ).all()
        flags = {sid: requested for sid, requested in rows}
        for sid in ids:
            task = _turn_tasks.get(sid)
            if task is None or task.done():
                continue
            if sid not in flags:
                logger.info("Chat turn %s no longer belongs to this worker (deleted or lease lost); stopping it.", sid)
                task.cancel()
            elif flags[sid] is not None:
                _user_cancelled.add(sid)
                task.cancel()

    async def _loop(self) -> None:
        settings = get_settings()
        # `_stopping` is the real exit condition; task.cancel() only makes it prompt. A cancel that lands inside a
        # DB driver call can come back as an ordinary exception (psycopg connect/rollback), which the `except
        # Exception` below swallows - without the flag the loop would then run forever and stop() would hang.
        while not self._stopping:
            try:
                await self.supervise_once()
                if time.monotonic() - self._last_reap >= settings.lease_heartbeat_seconds:
                    self._last_reap = time.monotonic()
                    await self.reap()
                await self.claim()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a DB hiccup must not kill the loop
                logger.warning("Chat turn supervisor tick failed.", exc_info=True)
            if self._stopping:
                return
            await asyncio.sleep(max(0.2, settings.chat_supervisor_seconds))

    async def start(self) -> None:
        """Lifespan / worker hook: reap leases left by dead workers, then supervise + claim in the background."""
        self._claiming = True
        self._stopping = False
        self._last_reap = time.monotonic()
        recovered = await self.reap()
        if recovered:
            logger.warning("Recovered %d chat turn(s) interrupted by a worker that stopped.", recovered)
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._loop(), name="chat-turn-supervisor")

    def stop_claiming(self) -> None:
        """SIGTERM drain, step 1: take no new turns."""
        self._claiming = False

    async def stop(self) -> None:
        """Shutdown / drain: stop claiming and supervising, then cancel running turns (each records "interrupted"
        and clears its marker + lease, so the user is offered a retry rather than a stuck spinner)."""
        self.stop_claiming()
        if self._supervisor is not None:
            self._stopping = True
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
            self._supervisor = None
        tasks = list(_turn_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_turn_runner: ChatTurnRunner | None = None


def get_turn_runner() -> ChatTurnRunner:
    global _turn_runner
    if _turn_runner is None:
        _turn_runner = ChatTurnRunner()
    return _turn_runner


async def _launch_background_turn(**kwargs) -> None:
    """Queues the turn on the session row. `ROLE=api` stops there (a worker claims it); otherwise this
    process claims it right away and runs it with the request's own injected clients."""
    runner = get_turn_runner()
    embedded = get_settings().role != "api"
    await runner.enqueue(
        kwargs["session_id"], kwargs["message"], kwargs["first_turn"], kwargs["turn_started_at"], claim=embedded
    )
    if embedded:
        runner.spawn(kwargs["session_id"], _background_turn(**kwargs))


async def cancel_background_turn(session_id: uuid.UUID) -> bool:
    """Requests cancellation of the session's queued/running background turn and returns whether there was one.

    The request is a DB flag, so it reaches the turn wherever it runs (another container): that worker's
    supervisor cancels it within `chat_supervisor_seconds` and the turn is recorded as "cancelled". A turn
    running in THIS process is also cancelled directly and awaited, so its in-flight Bedrock/search work stops
    being scheduled before the caller (e.g. DELETE) proceeds."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                update(ChatSession)
                .where(
                    ChatSession.id == session_id,
                    ChatSession.pending_turn_started_at.is_not(None),
                    ChatSession.turn_message.is_not(None),  # a queued/background job (inline turns have none)
                )
                .values(turn_cancel_requested_at=func.now())
                .execution_options(synchronize_session=False)  # SQLAlchemy < 2.0.52 #13439
                .returning(ChatSession.turn_lease_owner, ChatSession.pending_turn_started_at)
            )
        ).first()
        await db.commit()
    task = _turn_tasks.get(session_id)
    if task is not None and not task.done():
        _user_cancelled.add(session_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _user_cancelled.discard(session_id)
        return True
    if row is None:
        return False
    owner, started_at = row
    if owner is None:  # still queued: no worker has it, so there is nothing to stop - just record the outcome
        await _record_turn_failure(session_id, started_at, "cancelled", "This turn was cancelled.")
    return True


async def wait_for_background_turns() -> None:
    """Awaits every running background turn of this process (tests; graceful drain)."""
    while _turn_tasks:
        await asyncio.gather(*list(_turn_tasks.values()), return_exceptions=True)


async def shutdown_background_turns() -> None:
    """Server shutdown: cancel running turns (each records "interrupted" and clears its marker)."""
    await get_turn_runner().stop()


async def _accepted_response(session: ChatSession) -> ChatTurnRead:
    """The 202 body: the session's current state (best-effort) flagged as in progress."""
    turn = await get_session_state(str(session.id))
    response = (
        _turn_to_response(session.id, turn)
        if turn is not None
        else ChatTurnRead(session_id=session.id, status="gathering", draft={})
    )
    response.turn_in_progress = True
    response.title = session.title
    return response


def _turn_to_response(session_id: uuid.UUID, turn: BuilderTurnResult) -> ChatTurnRead:
    question = None
    if turn.question is not None:
        question = {
            "question": turn.question.get("question", ""),
            "options": turn.question.get("options", []),
            "missing_fields": turn.question.get("missing_fields", []),
        }
    return ChatTurnRead(
        session_id=session_id,
        status=turn.status,
        draft=turn.draft,
        question=question,
        similar_suggestions=turn.similar_suggestions,
        assistant_message=turn.assistant_note,
        # Always false here: every call site that reaches this normal turn-result path has
        # just finished its own graph call in the same request (turn_in_progress is only
        # ever true when a *different*, still-running request's marker is read back by
        # GET /chat/sessions/{id} — see that endpoint below).
        turn_in_progress=False,
    )


async def _persist_message(
    db: AsyncSession, session_id: uuid.UUID, role: ChatMessageRole, content: str, tool_calls=None
) -> None:
    db.add(ChatMessage(session_id=session_id, role=role, content=content, tool_calls=tool_calls))
    await db.flush()


async def _maybe_materialize(
    db: AsyncSession, session: ChatSession, turn: BuilderTurnResult, bedrock: BedrockClientProtocol
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """When the draft has just been confirmed, materialize it into a real Scorecard (see
    draft_materialize.py) and link it back onto the chat session. Best-effort: a
    materialization failure (e.g. a malformed hand-edited draft) does not fail the chat
    turn itself — the confirmed draft is still returned to the caller either way."""
    if turn.status != "confirmed":
        return None, None
    try:
        draft = ScorecardDraft.model_validate(turn.draft)
        # A session that already targets a scorecard (a "Refine with assistant" session,
        # or a plain session that has already been saved once) appends a new version to
        # it instead of creating a duplicate scorecard — see materialize_draft.
        scorecard, version = await materialize_draft(
            db, draft, owner_id=session.user_id, existing_scorecard_id=session.target_scorecard_id
        )
    except Exception:  # noqa: BLE001 — materialization is best-effort here, see docstring
        await db.rollback()
        return None, None
    session.target_scorecard_id = scorecard.id
    session.status = ChatSessionStatus.COMPLETED
    chat_user = await db.get(User, session.user_id)
    if chat_user is not None:
        await log_edit(
            db, scorecard.id, chat_user, "version_created",
            f"Saved version {version.version_number} from the assistant chat",
            entity_type="scorecard_version", entity_id=version.id, detail={"version_number": version.version_number},
        )
    await db.commit()

    # Fixes a real gap found during live testing: `upsert_scorecard_embedding` (see
    # app/ai/embeddings.py) had NO caller anywhere in the app — a chat-confirmed
    # scorecard never got a `scorecard_embeddings` row, so `check_similarity`'s "next
    # time a similar query comes in, suggest existing stuff" reuse feature (see
    # scorecard_builder.py) could never find it. Best-effort for the same reason
    # materialization above is: a Titan embedding failure must not fail the chat turn
    # that just successfully saved a real scorecard.
    try:
        await upsert_scorecard_embedding(db, version.id, bedrock)
    except Exception:  # noqa: BLE001 — best-effort, see comment above
        logger.warning(
            "Failed to embed newly materialized scorecard_version %s; it will not be "
            "discoverable via suggest-similar/check_similarity until a later save.",
            version.id,
            exc_info=True,
        )
        await db.rollback()

    return scorecard.id, version.id


async def chart_states(
    db: AsyncSession, user: User, sessions: list[ChatSession]
) -> dict[uuid.UUID, tuple[str, uuid.UUID | None, bool]]:
    """Per chat session: (chart_state, chart id the caller may use, caller can restore it).

    `none` = no chart linked (yet); `active` = the chart is live; `trashed` = it is in the trash (its id is only
    handed to the chart's OWNER, who can restore it - everybody else would just hit a 404); `deleted` = the session
    built a chart (it is COMPLETED) but the chart is gone (the FK is SET NULL on delete/purge)."""
    ids = {s.target_scorecard_id for s in sessions if s.target_scorecard_id is not None}
    cards: dict[uuid.UUID, Scorecard] = {}
    if ids:
        cards = {
            c.id: c for c in (await db.execute(select(Scorecard).where(Scorecard.id.in_(ids)))).scalars().all()
        }
    out: dict[uuid.UUID, tuple[str, uuid.UUID | None, bool]] = {}
    for s in sessions:
        card = cards.get(s.target_scorecard_id) if s.target_scorecard_id is not None else None
        if card is None:
            out[s.id] = ("deleted" if s.status == ChatSessionStatus.COMPLETED else "none", None, False)
        elif card.deleted_at is None:
            out[s.id] = ("active", card.id, False)
        elif card.owner_id == user.id:
            out[s.id] = ("trashed", card.id, True)
        else:
            out[s.id] = ("trashed", None, False)
    return out


@router.get("/sessions", response_model=list[ChatSessionRead])
async def list_chat_sessions(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChatSessionRead]:
    """Additive (Wave 3 integration): the frontend's Home "continue where you left off"
    list and the Chat sidebar both need a real list of chat sessions — this was missing
    from the Cycle 1c AI-core pass, which only exposed per-session endpoints."""
    stmt = (
        select(ChatSession).where(ChatSession.user_id == current_user.id).order_by(ChatSession.last_activity_at.desc())
    )
    result = await db.execute(stmt.offset(skip).limit(limit))
    sessions = list(result.scalars().all())
    states = await chart_states(db, current_user, sessions)
    out: list[ChatSessionRead] = []
    for s in sessions:
        state, visible_id, can_restore = states[s.id]
        item = ChatSessionRead.model_validate(s)
        item.chart_state, item.chart_can_restore, item.target_scorecard_id = state, can_restore, visible_id
        out.append(item)
    return out


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
async def list_chat_messages(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[ChatMessage]:
    """Additive (Wave 3 integration): real persisted chat history for a session, so the
    frontend can render a resumed conversation (`ChatTurnRead` only carries current
    LangGraph turn/draft state, not the message log)."""
    await get_owned_chat_session(db, current_user, session_id)
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "/sessions",
    response_model=ChatTurnRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("chat", lambda s: s.rate_limit_chat, per_user=True))],
)
async def start_chat_session(
    payload: ChatSessionStart,
    response: Response,
    wait: bool | None = Query(
        default=None,
        description="true: run the turn inline and return the full result (200/201); false/omitted: 202 and poll.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    bedrock: BedrockClientProtocol = Depends(get_bedrock_client),
    web_search: WebSearchClientProtocol = Depends(get_web_search_client),
    jev: JevClientProtocol = Depends(get_jev_client),
) -> ChatTurnRead:
    message = payload.message.strip()
    if payload.target_scorecard_id is None and not message:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`message` is required unless `target_scorecard_id` is given.",
        )

    if payload.target_scorecard_id is not None:
        return await _start_refine_session(
            db, current_user, bedrock, web_search, jev, payload.target_scorecard_id, message,
            response=response, inline=_use_inline_turns(wait),
        )

    # Originally (see prior bug writeup): a brand-new "start fresh" session used to be
    # committed to chat_sessions/chat_messages only AFTER the first Bedrock call
    # succeeded, so a first-message failure left nothing behind — but that also meant
    # there was NOTHING for a concurrent GET (a refreshed tab, or this feature's own
    # turn-events poll) to find while that first call was still running. This is, in
    # fact, the ONLY call site where the multi-agent research fan-out (`research_kpis`,
    # see scorecard_builder.py) ever actually runs — `research_done` is already True on
    # every other path into the graph — so it is the single most important place for live
    # turn-trace visibility to work. The session row (and pending_turn_started_at marker)
    # is therefore now created up front, before the graph call, exactly like every other
    # turn endpoint below; the original "leaves nothing behind on failure" guarantee is
    # preserved by deleting that same row (cascading to any chat_turn_events it
    # accumulated) if the graph call raises, so a failed first message still leaves no
    # visible session for the client to find — only a turn that actually succeeds does.
    #
    # Sidebar-live-update fix: prefer the client-supplied `session_id` (see
    # ChatSessionStart.session_id docstring) so the id the frontend already started
    # polling with — and already put in the sidebar as a placeholder — is the SAME id
    # this row gets, instead of a server-only id the client can't learn until this whole
    # (long-running) request returns.
    session_id = payload.session_id or uuid.uuid4()
    settings = get_settings()
    session = ChatSession(id=session_id, user_id=current_user.id)
    db.add(session)
    try:
        await db.commit()
    except IntegrityError as exc:  # a client-chosen id that is already taken (by anyone): same answer for all
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This session id is already in use.") from exc
    if not _use_inline_turns(wait):
        # Background mode: the title call and the whole graph run happen in the task (see
        # `_background_turn`); the client polls GET /chat/sessions/{id}. A failed first turn keeps the
        # session row (so the error is readable); inline mode below still deletes it.
        # The user's message is persisted in the SAME transaction that marks the turn in progress
        # (`_mark_turn_in_progress` commits), so a reload / navigation right after the 202 — or a
        # failed/cancelled first turn — still sees it; the success path must not write it again.
        await _persist_message(db, session_id, ChatMessageRole.USER, payload.message)
        turn_started_at = await _mark_new_turn(db, session_id)
        await _launch_background_turn(
            session_id=session_id, message=payload.message, first_turn=True, persist_user_message=False,
            turn_started_at=turn_started_at, bedrock=bedrock, web_search=web_search, jev=jev,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return await _accepted_response(session)
    # Issue 1 (see task notes): generate the session's real title as the VERY FIRST thing
    # on its first turn — one fast/cheap Bedrock call, well before the heavier LangGraph
    # work below (check_similarity/research_kpis/propose_kpis) even starts. Persisted
    # immediately (see _generate_and_persist_title) so it's visible to a concurrent
    # request long before this one returns. If the graph call below fails and this
    # session row is deleted, the title is deleted right along with it — no orphaned
    # title left behind either.
    await _generate_and_persist_title(db, session, bedrock, payload.message)
    turn_started_at = await _mark_new_turn(db, session_id)
    try:
        turn = await start_session(
            str(session_id),
            payload.message,
            bedrock,
            chat_model_id=settings.bedrock_chat_model_id,
            db=db,
            web_search_client=web_search,
            turn_started_at=turn_started_at,
            jev_client=jev,
        )
    except BedrockUnavailableError as exc:
        await db.execute(
            delete(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
        )
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception:
        await db.execute(
            delete(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == current_user.id)
        )
        await db.commit()
        raise

    # The graph call above can legitimately run for minutes (research fan-out); it's
    # possible for the session to have been deleted out from under it in the meantime —
    # e.g. a concurrent `DELETE /chat/sessions/{id}` against this same session_id from
    # another tab/client while this turn was still in flight. Every write below has a
    # `chat_sessions.id` foreign key, so that shows up as an `IntegrityError` (previously
    # an unhandled 500 with a raw SQL traceback — confirmed live: emit_turn_event's own
    # best-effort writes already degrade gracefully in this situation (see its "never
    # raise" contract), but nothing downstream of the graph call did). Surface it as a
    # clean, expected 409 instead of leaking an internal DB error.
    try:
        await _persist_message(db, session.id, ChatMessageRole.USER, payload.message)

        tool_calls = None
        if turn.question:
            tool_calls = {"question": turn.question}
        elif turn.similar_suggestions:
            tool_calls = {"similar_suggestions": turn.similar_suggestions}
        await _persist_message(
            db, session.id, ChatMessageRole.ASSISTANT, turn.assistant_note or "", tool_calls=tool_calls
        )
        scorecard_id, version_id = await _maybe_materialize(db, session, turn, bedrock)
        await _clear_turn_in_progress(db, session.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "This chat session was deleted while your message was still being "
                "processed, so the result could not be saved."
            ),
        ) from exc

    response = _turn_to_response(session.id, turn)
    response.materialized_scorecard_id = scorecard_id
    response.materialized_scorecard_version_id = version_id
    response.title = session.title
    return response


async def _start_refine_session(
    db: AsyncSession,
    current_user: User,
    bedrock: BedrockClientProtocol,
    web_search: WebSearchClientProtocol,
    jev: JevClientProtocol,
    scorecard_id: uuid.UUID,
    message: str,
    *,
    response: Response,
    inline: bool,
) -> ChatTurnRead:
    """"Refine with assistant": open a session whose draft is pre-populated from the
    scorecard's current version (see `draft_from_scorecard` / `seed_session`). Seeding
    never calls the model, so the session (and its loaded draft) is created successfully
    even when Bedrock is unavailable; only an optional first `message` needs Bedrock."""
    scorecard = await get_accessible_scorecard(db, current_user, scorecard_id)
    if scorecard.current_version_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Scorecard has no current version to refine."
        )
    version = (
        await db.execute(
            select(ScorecardVersion)
            .where(ScorecardVersion.id == scorecard.current_version_id)
            .options(selectinload(ScorecardVersion.kpi_nodes).selectinload(KpiNode.guidelines))
        )
    ).scalar_one()

    draft = draft_from_scorecard(scorecard, list(version.kpi_nodes), version.scoring_formula)
    leaf_count = len({n.id for n in version.kpi_nodes} - {n.parent_id for n in version.kpi_nodes})
    context_message = (
        f'I want to refine the existing scorecard "{scorecard.name}" (currently version '
        f"{version.version_number}). The current draft below is loaded from it."
    )
    assistant_note = (
        f'I\'ve loaded "{scorecard.name}" (version {version.version_number}, '
        f"{len(version.kpi_nodes)} KPIs, {leaf_count} scored) into the draft on the right. "
        "Tell me what you'd like to change — for example rename or add a KPI, rebalance "
        "weights, or rewrite a guideline. When you're happy, say so and I'll save it as "
        f"version {version.version_number + 1}; version {version.version_number} and its "
        "evaluations stay exactly as they are."
    )

    session = ChatSession(
        user_id=current_user.id,
        target_scorecard_id=scorecard.id,
        context_summary=f'Refining "{scorecard.name}"',
        # Deterministic — no Bedrock call needed (or wanted: the scorecard being refined
        # already gives a perfectly specific title for free) — see Issue 1's task notes.
        title=f'Refining "{scorecard.name}"',
    )
    db.add(session)
    await db.flush()
    await _persist_message(db, session.id, ChatMessageRole.ASSISTANT, assistant_note)
    await db.commit()

    turn = await seed_session(str(session.id), draft, context_message, assistant_note)

    if message and not inline:
        await _persist_message(db, session.id, ChatMessageRole.USER, message)
        await db.commit()
        turn_started_at = await _mark_new_turn(db, session.id)
        await _launch_background_turn(
            session_id=session.id, message=message, first_turn=False, persist_user_message=False,
            turn_started_at=turn_started_at, bedrock=bedrock, web_search=web_search, jev=jev,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        accepted = _turn_to_response(session.id, turn)
        accepted.turn_in_progress = True
        accepted.title = session.title
        return accepted

    if message:
        await _persist_message(db, session.id, ChatMessageRole.USER, message)
        await db.commit()
        turn_started_at = await _mark_new_turn(db, session.id)
        settings = get_settings()
        try:
            turn = await send_message(
                str(session.id),
                message,
                bedrock,
                chat_model_id=settings.bedrock_chat_model_id,
                db=db,
                web_search_client=web_search,
                turn_started_at=turn_started_at,
                jev_client=jev,
            )
        except BedrockUnavailableError as exc:
            await _clear_turn_in_progress(db, session.id)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except Exception:
            await _clear_turn_in_progress(db, session.id)
            raise
        # See the identical try/except in start_chat_session for why this is needed: the
        # session may have been deleted concurrently while the graph call above ran.
        try:
            tool_calls = {"question": turn.question} if turn.question else None
            await _persist_message(
                db, session.id, ChatMessageRole.ASSISTANT, turn.assistant_note or "", tool_calls=tool_calls
            )
            scorecard_out, version_out = await _maybe_materialize(db, session, turn, bedrock)
            await _clear_turn_in_progress(db, session.id)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=(
                    "This chat session was deleted while your message was still being "
                    "processed, so the result could not be saved."
                ),
            ) from exc
        response = _turn_to_response(session.id, turn)
        response.materialized_scorecard_id = scorecard_out
        response.materialized_scorecard_version_id = version_out
        response.title = session.title
        return response

    no_message_response = _turn_to_response(session.id, turn)
    no_message_response.title = session.title
    return no_message_response


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatTurnRead,
    dependencies=[Depends(rate_limit("chat", lambda s: s.rate_limit_chat, per_user=True))],
)
async def send_chat_message(
    session_id: uuid.UUID,
    payload: ChatMessageCreate,
    response: Response,
    wait: bool | None = Query(
        default=None,
        description="true: run the turn inline and return the full result (200); false/omitted: 202 and poll.",
    ),
    db: AsyncSession = Depends(get_db),
    bedrock: BedrockClientProtocol = Depends(get_bedrock_client),
    web_search: WebSearchClientProtocol = Depends(get_web_search_client),
    jev: JevClientProtocol = Depends(get_jev_client),
    current_user: User = Depends(get_current_user),
) -> ChatTurnRead:
    session = await get_owned_chat_session(db, current_user, session_id)

    if session.turn_in_progress:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="The assistant is still working on your previous message."
        )

    # "Try again" after a failed/cancelled turn re-sends the SAME text: the original user message is
    # already persisted (a first turn persists it up front), so don't write a duplicate.
    last = await db.scalar(
        select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.desc()).limit(1)
    )
    is_retry = (
        session.last_turn_error_code is not None
        and last is not None
        and last.role == ChatMessageRole.USER
        and last.content == payload.message
    )
    # Claim the session first (atomic; 409 if another request just started a turn), then record the message.
    turn_started_at = await _mark_turn_in_progress(db, session.id, require_idle=True)
    if not is_retry:
        await _persist_message(db, session.id, ChatMessageRole.USER, payload.message)
        await db.commit()

    if not _use_inline_turns(wait):
        await _launch_background_turn(
            session_id=session.id, message=payload.message, first_turn=False, persist_user_message=False,
            turn_started_at=turn_started_at, bedrock=bedrock, web_search=web_search, jev=jev,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return await _accepted_response(session)

    settings = get_settings()
    try:
        turn = await send_message(
            str(session.id),
            payload.message,
            bedrock,
            chat_model_id=settings.bedrock_chat_model_id,
            db=db,
            web_search_client=web_search,
            turn_started_at=turn_started_at,
            jev_client=jev,
        )
    except BedrockUnavailableError as exc:
        await _clear_turn_in_progress(db, session.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception:
        # Belt-and-suspenders: even an unexpected crash mid-turn must not leave the
        # "still working" marker set forever for a *live* process (STALE_TURN_TIMEOUT_
        # SECONDS handles the case where the whole process dies instead).
        await _clear_turn_in_progress(db, session.id)
        raise

    # See the identical try/except in start_chat_session above for why this is needed: the
    # graph call can run for minutes, and the session may have been deleted concurrently
    # (e.g. a `DELETE /chat/sessions/{id}` from another tab) while it ran.
    try:
        tool_calls = None
        if turn.question:
            tool_calls = {"question": turn.question}
        elif turn.similar_suggestions:
            tool_calls = {"similar_suggestions": turn.similar_suggestions}
        await _persist_message(
            db, session.id, ChatMessageRole.ASSISTANT, turn.assistant_note or "", tool_calls=tool_calls
        )
        scorecard_id, version_id = await _maybe_materialize(db, session, turn, bedrock)
        await _clear_turn_in_progress(db, session.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "This chat session was deleted while your message was still being "
                "processed, so the result could not be saved."
            ),
        ) from exc

    response = _turn_to_response(session.id, turn)
    response.materialized_scorecard_id = scorecard_id
    response.materialized_scorecard_version_id = version_id
    response.title = session.title
    return response


@router.get("/sessions/{session_id}", response_model=ChatTurnRead)
async def get_chat_session(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> ChatTurnRead:
    """Also the refresh-recovery endpoint (Part A): `response.turn_in_progress` tells a
    reloaded chat page whether a turn is currently running server-side for this session
    (see `ChatSession.turn_in_progress` / `pending_turn_started_at`), so the frontend can
    show a persistent "still working" indicator and poll this endpoint instead of
    rendering a blank composer as if nothing were happening."""
    session = await get_owned_chat_session(db, current_user, session_id)
    chart_state, visible_chart_id, can_restore = (await chart_states(db, current_user, [session]))[session.id]

    in_progress = session.turn_in_progress
    turn_error = None if in_progress else session.last_turn_error
    turn_error_code = None if in_progress else session.last_turn_error_code
    turn = await get_session_state(str(session_id))
    if turn is None:
        if in_progress or turn_error:
            # A turn is running but has not produced its first LangGraph checkpoint yet
            # (e.g. still inside the initial research fan-out) — report "still working"
            # with an empty draft rather than 404ing, so a refreshed page can poll here
            # instead of erroring out.
            return ChatTurnRead(
                session_id=session_id,
                status="gathering",
                draft={},
                turn_in_progress=in_progress,
                turn_error=turn_error,
                turn_error_code=turn_error_code,
                # The title-generation call (see start_chat_session) runs and persists
                # BEFORE the graph call that produces the first checkpoint this branch is
                # covering the absence of — so it's already reliably set here, even this
                # early. This is exactly the "second connection sees the title before the
                # first request returns" path the task's live-verification asks for.
                title=session.title,
                chart_state=chart_state,
                chart_can_restore=can_restore,
            )
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Chat session has no LangGraph state yet."
        )
    response = _turn_to_response(session_id, turn)
    response.turn_in_progress = in_progress
    response.turn_error = turn_error
    response.turn_error_code = turn_error_code
    response.title = session.title
    # Only report a materialized scorecard once something was actually saved: a
    # "Refine with assistant" session carries target_scorecard_id from the start, before
    # anything has been confirmed.
    response.chart_state, response.chart_can_restore = chart_state, can_restore
    if session.status == ChatSessionStatus.COMPLETED:
        # a trashed / deleted chart must not be offered as a link (it would be a 404 dead end)
        response.materialized_scorecard_id = visible_chart_id if chart_state == "active" else None
    return response


@router.post("/sessions/{session_id}/cancel", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def cancel_chat_turn(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Stops the session's running background turn (no-op if none is running); the session is
    kept and `turn_error_code` becomes "cancelled" so a client can offer a retry."""
    await get_owned_chat_session(db, current_user, session_id)
    await cancel_background_turn(session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_chat_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Deletes a chat session (the hover-delete action in the sidebar's session list).
    Only the session's owner can delete it (anyone else gets a 404).

    Cascades: `chat_messages` and `chat_turn_events` both have `session_id` FKs with
    `ondelete="CASCADE"` (see their models), so deleting the `chat_sessions` row removes
    them at the DB level automatically — no explicit query needed for either. LangGraph's
    OWN checkpoint tables (`checkpoints`/`checkpoint_writes`/`checkpoint_blobs`) are a
    separate schema it manages itself (see `scorecard_builder.py::GraphManager`), so those
    are cleaned up separately via `delete_session_checkpoints`, best-effort: a failure
    there must not leave the relational row (and thus the now-undeletable-looking session)
    behind, and a stray orphaned checkpoint row for an id nothing references any more is
    harmless (never read again, since nothing can look it up).
    """
    session = await get_owned_chat_session(db, current_user, session_id)

    # Stop an abandoned/still-running background turn first so it stops consuming Bedrock capacity.
    await cancel_background_turn(session_id)
    await db.refresh(session)
    await db.delete(session)
    await db.commit()

    try:
        await delete_session_checkpoints(str(session_id))
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        logger.warning(
            "Failed to delete LangGraph checkpoint rows for deleted chat session_id=%s; "
            "the relational row is already gone.",
            session_id,
            exc_info=True,
        )


@router.get("/sessions/{session_id}/turn-events", response_model=list[ChatTurnEventRead])
async def get_chat_turn_events(
    session_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[ChatTurnEvent]:
    """The live-trace event log behind the "what is the assistant doing right now" UI
    (replaces the old generic "Assistant is thinking…" indicator — see
    `app/models/chat_turn_event.py` and the nodes in `app/ai/scorecard_builder.py` that
    write these rows as the pipeline actually runs, including each concurrently-running
    research agent's own events).

    Scoped to the CURRENT/most recent turn only, never the whole session's history: reads
    the max `turn_started_at` recorded for this session and returns only rows matching it
    (ordered by `created_at`, so the frontend can render them chronologically / grouped by
    `actor`). Works identically whether a turn is still in progress (poll this alongside
    `turn_in_progress` — see `GET /chat/sessions/{id}`) or has just finished (the same
    "most recent turn" rows are still returned, so a page load/refresh right after a turn
    completes still shows the full trace instead of nothing).

    `_mark_turn_in_progress` (see above) already deletes the PREVIOUS turn's events the
    moment a new turn starts, so in practice at most one turn's worth of rows exists per
    session at any time — the `turn_started_at` scoping here is a second, redundant guard
    against ever reading stale history, not the only thing bounding table growth."""
    await get_owned_chat_session(db, current_user, session_id)

    latest_turn_started_at = (
        await db.execute(
            select(func.max(ChatTurnEvent.turn_started_at)).where(ChatTurnEvent.session_id == session_id)
        )
    ).scalar_one_or_none()
    if latest_turn_started_at is None:
        return []

    result = await db.execute(
        select(ChatTurnEvent)
        .where(
            ChatTurnEvent.session_id == session_id,
            ChatTurnEvent.turn_started_at == latest_turn_started_at,
        )
        .order_by(ChatTurnEvent.created_at)
    )
    return list(result.scalars().all())
