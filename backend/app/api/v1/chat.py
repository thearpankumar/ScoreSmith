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
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from app.config import get_settings
from app.db import AsyncSessionLocal, get_db
from app.deps import get_bedrock_client, get_current_user, get_jev_client, get_web_search_client
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.chat_turn_event import ChatTurnEvent
from app.models.enums import ChatMessageRole, ChatSessionStatus
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.schemas.chat import (
    ChatMessageCreate,
    ChatMessageRead,
    ChatSessionRead,
    ChatSessionStart,
    ChatTurnEventRead,
    ChatTurnRead,
)

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


async def _mark_turn_in_progress(db: AsyncSession, session_id: uuid.UUID) -> datetime:
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
    await db.execute(delete(ChatTurnEvent).where(ChatTurnEvent.session_id == session_id))
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(pending_turn_started_at=started_at, last_turn_error=None, last_turn_error_code=None)
    )
    await db.commit()
    return started_at


async def _clear_turn_in_progress(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(ChatSession).where(ChatSession.id == session_id).values(pending_turn_started_at=None)
    )
    await db.commit()


async def _generate_and_persist_title(
    db: AsyncSession, session: ChatSession, bedrock: BedrockClientProtocol, first_message: str
) -> str | None:
    """Issue 1 (see task notes): a brand-new session's title must be generated as the
    VERY FIRST thing on its first turn — before check_similarity/research_kpis/
    propose_kpis (the heavier LangGraph work — see scorecard_builder.py) ever run — via
    ONE fast/cheap Bedrock call (settings.judge_model_id, not the heavier chat
    model), and persisted immediately so a concurrent request (a different tab's session
    list poll, or this same request's own eventual response) can see it well before the
    graph call returns. Best-effort: never raises, never blocks session creation on a
    cosmetic feature — see generate_session_title's own "never raise" contract."""
    settings = get_settings()
    title = await asyncio.to_thread(
        generate_session_title, bedrock, settings.judge_model_id, first_message
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
#   - POST marks the turn in progress (`pending_turn_started_at`, durable) and starts an
#     `asyncio.Task` on the server's event loop, then returns 202 immediately.
#   - The task opens its OWN `AsyncSession` (the request-scoped one is closed once the response
#     is sent), runs the same LangGraph turn (`start_session`/`send_message`, whose
#     `AsyncPostgresSaver` is a process-lifetime singleton on the same loop), persists the
#     messages, and clears the marker. Live progress is the existing `chat_turn_events` rows.
#   - The client polls `GET /chat/sessions/{id}` (state + `turn_in_progress` + `turn_error`) and
#     `GET .../turn-events`. Polling over SSE: all state is already durable in Postgres, so a
#     refresh/another tab/a server restart just resumes polling; SSE would add a held-open
#     connection per tab (proxy buffering/idle timeouts, EventSource cannot send the dev-auth
#     header) and a second progress channel for no gain at 1.5s granularity.
#   - Failure is recorded in `chat_sessions.last_turn_error[_code]` (migration 0009) and the
#     marker cleared. A hard wall-clock limit (`settings.chat_turn_timeout_seconds`) bounds the
#     task. Server shutdown cancels running tasks (recording "interrupted"); a crash/--reload
#     restart leaves orphaned markers that `recover_interrupted_turns` clears at startup
#     (assumes ONE API process, as in infra/docker-compose.yml: with several workers a worker's
#     startup would also clear markers of turns still running in the others — the staleness
#     cutoff is then the only guard). Tasks are kept in a set so they are not garbage-collected
#     mid-run (asyncio only holds weak references to tasks).
#   - Windows: the task runs on the SAME event loop as the server (set to the selector policy in
#     app/main.py before the loop exists); no thread/loop is created here.

_background_turns: set[asyncio.Task[None]] = set()
# session id -> its running background turn, so DELETE / cancel can stop an abandoned turn (a
# killed client does not stop the server-side task; orphans would keep consuming Bedrock capacity).
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
                .values(pending_turn_started_at=None, last_turn_error=message, last_turn_error_code=code)
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
                    chat_model_id=settings.chat_model_id,
                    db=db,
                    web_search_client=web_search,
                    turn_started_at=turn_started_at,
                    jev_client=jev,
                )
            await _persist_turn_result(
                db, session, turn, bedrock, user_message=message if persist_user_message else None
            )
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


def _launch_background_turn(**kwargs) -> None:
    task = asyncio.create_task(_background_turn(**kwargs), name=f"chat-turn-{kwargs['session_id']}")
    _background_turns.add(task)
    session_id = kwargs["session_id"]
    _turn_tasks[session_id] = task

    def _done(t: asyncio.Task[None]) -> None:
        _background_turns.discard(t)
        if _turn_tasks.get(session_id) is t:
            del _turn_tasks[session_id]

    task.add_done_callback(_done)


async def cancel_background_turn(session_id: uuid.UUID) -> bool:
    """Cancels the session's running background turn (if any) and waits for it to unwind, so its
    in-flight Bedrock/search work stops being scheduled. Returns whether a turn was running."""
    task = _turn_tasks.get(session_id)
    if task is None or task.done():
        return False
    _user_cancelled.add(session_id)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _user_cancelled.discard(session_id)
    return True


async def wait_for_background_turns() -> None:
    """Awaits every running background turn (tests; graceful drain)."""
    while _background_turns:
        await asyncio.gather(*list(_background_turns), return_exceptions=True)


async def shutdown_background_turns() -> None:
    """Server shutdown: cancel running turns (each records "interrupted" and clears its marker)."""
    tasks = list(_background_turns)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def recover_interrupted_turns() -> int:
    """Startup recovery: any session still marked in-progress when this process starts belongs to
    a turn that died with the previous process (crash, kill, --reload) — no task of THIS process
    can own it yet. Clear the marker and record why, so the UI stops showing "still working" and
    offers a retry instead of waiting out the staleness cutoff. Returns how many were recovered."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(ChatSession)
                .where(ChatSession.pending_turn_started_at.is_not(None))
                .values(
                    pending_turn_started_at=None,
                    last_turn_error="The server restarted while working on your message. Please send it again.",
                    last_turn_error_code="interrupted",
                )
            )
            await db.commit()
            return result.rowcount or 0
    except Exception:  # noqa: BLE001 — e.g. DB not reachable/migrated yet; never block startup
        logger.warning("Could not recover interrupted chat turns at startup.", exc_info=True)
        return 0


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


@router.get("/sessions", response_model=list[ChatSessionRead])
async def list_chat_sessions(
    skip: int = 0,
    limit: int = 100,
    user_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[ChatSession]:
    """Additive (Wave 3 integration): the frontend's Home "continue where you left off"
    list and the Chat sidebar both need a real list of chat sessions — this was missing
    from the Cycle 1c AI-core pass, which only exposed per-session endpoints."""
    stmt = select(ChatSession).order_by(ChatSession.last_activity_at.desc())
    if user_id is not None:
        stmt = stmt.where(ChatSession.user_id == user_id)
    result = await db.execute(stmt.offset(skip).limit(limit))
    return list(result.scalars().all())


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
async def list_chat_messages(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[ChatMessage]:
    """Additive (Wave 3 integration): real persisted chat history for a session, so the
    frontend can render a resumed conversation (`ChatTurnRead` only carries current
    LangGraph turn/draft state, not the message log)."""
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    return list(result.scalars().all())


@router.post("/sessions", response_model=ChatTurnRead, status_code=status.HTTP_201_CREATED)
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
    await db.commit()
    if not _use_inline_turns(wait):
        # Background mode: the title call and the whole graph run happen in the task (see
        # `_background_turn`); the client polls GET /chat/sessions/{id}. A failed first turn keeps the
        # session row (so the error is readable); inline mode below still deletes it.
        # The user's message is persisted in the SAME transaction that marks the turn in progress
        # (`_mark_turn_in_progress` commits), so a reload / navigation right after the 202 — or a
        # failed/cancelled first turn — still sees it; the success path must not write it again.
        await _persist_message(db, session_id, ChatMessageRole.USER, payload.message)
        turn_started_at = await _mark_turn_in_progress(db, session_id)
        _launch_background_turn(
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
    turn_started_at = await _mark_turn_in_progress(db, session_id)
    try:
        turn = await start_session(
            str(session_id),
            payload.message,
            bedrock,
            chat_model_id=settings.chat_model_id,
            db=db,
            web_search_client=web_search,
            turn_started_at=turn_started_at,
            jev_client=jev,
        )
    except BedrockUnavailableError as exc:
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception:
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
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
    scorecard = await db.get(Scorecard, scorecard_id)
    if scorecard is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard not found.")
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
        turn_started_at = await _mark_turn_in_progress(db, session.id)
        _launch_background_turn(
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
        turn_started_at = await _mark_turn_in_progress(db, session.id)
        settings = get_settings()
        try:
            turn = await send_message(
                str(session.id),
                message,
                bedrock,
                chat_model_id=settings.chat_model_id,
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


@router.post("/sessions/{session_id}/messages", response_model=ChatTurnRead)
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
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> ChatTurnRead:
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

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
    if not is_retry:
        await _persist_message(db, session.id, ChatMessageRole.USER, payload.message)
    await db.commit()
    turn_started_at = await _mark_turn_in_progress(db, session.id)

    if not _use_inline_turns(wait):
        _launch_background_turn(
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
            chat_model_id=settings.chat_model_id,
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
async def get_chat_session(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ChatTurnRead:
    """Also the refresh-recovery endpoint (Part A): `response.turn_in_progress` tells a
    reloaded chat page whether a turn is currently running server-side for this session
    (see `ChatSession.turn_in_progress` / `pending_turn_started_at`), so the frontend can
    show a persistent "still working" indicator and poll this endpoint instead of
    rendering a blank composer as if nothing were happening."""
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

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
    if session.status == ChatSessionStatus.COMPLETED:
        response.materialized_scorecard_id = session.target_scorecard_id
    return response


@router.post("/sessions/{session_id}/cancel", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def cancel_chat_turn(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> None:
    """Stops the session's running background turn (no-op if none is running); the session is
    kept and `turn_error_code` becomes "cancelled" so a client can offer a retry."""
    if await db.get(ChatSession, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
    await cancel_background_turn(session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_chat_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> None:
    """Deletes a chat session (the hover-delete action in the sidebar's session list).
    Follows the same auth pattern as `DELETE /scorecards/{id}` / `DELETE /evaluations/{id}`
    (just the dev-auth-stub dependency — see app/deps.py; neither of those routes filters
    by ownership either, so this doesn't add a check they don't have).

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
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

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
async def get_chat_turn_events(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[ChatTurnEvent]:
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
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

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
