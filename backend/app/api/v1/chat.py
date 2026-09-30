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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select, update
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
from app.db import get_db
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
# The chat-turn endpoints below block synchronously for the whole LangGraph run (no
# background job queue) — see the module docstring. `ChatSession.pending_turn_started_at`
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
        .values(pending_turn_started_at=started_at)
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
            db, current_user, bedrock, web_search, jev, payload.target_scorecard_id, message
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
    session_id = uuid.uuid4()
    settings = get_settings()
    session = ChatSession(id=session_id, user_id=current_user.id)
    db.add(session)
    await db.commit()
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
            chat_model_id=settings.bedrock_chat_model_id,
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
        tool_calls = {"question": turn.question} if turn.question else None
        await _persist_message(
            db, session.id, ChatMessageRole.ASSISTANT, turn.assistant_note or "", tool_calls=tool_calls
        )
        scorecard_out, version_out = await _maybe_materialize(db, session, turn, bedrock)
        await _clear_turn_in_progress(db, session.id)
        await db.commit()
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
    db: AsyncSession = Depends(get_db),
    bedrock: BedrockClientProtocol = Depends(get_bedrock_client),
    web_search: WebSearchClientProtocol = Depends(get_web_search_client),
    jev: JevClientProtocol = Depends(get_jev_client),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> ChatTurnRead:
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Chat session not found.")

    await _persist_message(db, session.id, ChatMessageRole.USER, payload.message)
    await db.commit()
    turn_started_at = await _mark_turn_in_progress(db, session.id)

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
    turn = await get_session_state(str(session_id))
    if turn is None:
        if in_progress:
            # A turn is running but has not produced its first LangGraph checkpoint yet
            # (e.g. still inside the initial research fan-out) — report "still working"
            # with an empty draft rather than 404ing, so a refreshed page can poll here
            # instead of erroring out.
            return ChatTurnRead(
                session_id=session_id,
                status="gathering",
                draft={},
                turn_in_progress=True,
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
    response.title = session.title
    # Only report a materialized scorecard once something was actually saved: a
    # "Refine with assistant" session carries target_scorecard_id from the start, before
    # anything has been confirmed.
    if session.status == ChatSessionStatus.COMPLETED:
        response.materialized_scorecard_id = session.target_scorecard_id
    return response


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
