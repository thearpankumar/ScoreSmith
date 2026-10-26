from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.enums import ChatMessageRole, ChatSessionStatus
from app.schemas.common import ORMBase

ChartState = Literal["none", "active", "trashed", "deleted"]


class ChatSessionStart(BaseModel):
    """Start a chat-builder session.

    - Plain "start fresh" (unchanged): `message` is required; `target_scorecard_id` omitted.
    - "Refine with assistant": pass `target_scorecard_id`. The session's draft is seeded
      from that scorecard's current version (no model call needed), and a later confirm
      saves the result as a NEW version of that same scorecard. `message` is optional in
      this mode — omit it to just open the seeded session (works without Bedrock); pass
      it to also run a first assistant turn immediately."""

    message: str = ""
    target_scorecard_id: uuid.UUID | None = None
    # Sidebar-live-update fix: the "start fresh" endpoint blocks synchronously for the
    # whole LangGraph run (see start_chat_session's docstring), so the frontend has no
    # real session id to adopt/poll with until this request finally returns — the one
    # gap ChatWorkspace's own docstring used to call out as "can't be polled". Letting the
    # client generate the id up front (a plain random UUID, no collision handling needed)
    # and pass it here means the session row (+ its fast title-generation call, done
    # before this long request even starts its graph work) is reachable via
    # `GET /chat/sessions/{id}` moments after the client fires this POST, while it's still
    # in flight — see ChatWorkspace.runAssistantTurn for the client side. Optional and
    # ignored (a server-generated id is used instead) for any other caller that doesn't
    # send one.
    session_id: uuid.UUID | None = None
    # The chat model the user picked (OpenRouter only; validated against the admin's enabled list, else the
    # default is used — see app/ai/chat_models.py) and files uploaded for this message (app/api/v1/chat_attachments.py).
    model: str | None = Field(default=None, max_length=200)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)


class ChatMessageCreate(BaseModel):
    message: str
    model: str | None = Field(default=None, max_length=200)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)


class ChatAttachmentRead(ORMBase):
    """A file attached to a chat message (or uploaded for the next one). `outline` summarises what was extracted:
    pages / sheets / rows / whether the text was cut to fit (see app/ai/attachments.py)."""

    id: uuid.UUID
    filename: str
    kind: str
    size_bytes: int
    status: str
    error: str | None = None
    outline: dict | None = None
    token_estimate: int | None = None
    message_id: uuid.UUID | None = None
    created_at: datetime


class ChatClarificationQuestion(BaseModel):
    question: str
    options: list[str] = []
    missing_fields: list[str] = []


class ChatSimilarSuggestion(BaseModel):
    """One reuse-suggestion card (Use as-is / Adapt / Start fresh), surfaced by the
    LangGraph `check_similarity` node (see `app/ai/scorecard_builder.py`) before
    `propose_kpis` ever runs — the chat-flow-integrated counterpart to the standalone
    `POST /scorecards/suggest-similar` endpoint's `SuggestSimilarResult`."""

    scorecard_id: uuid.UUID
    scorecard_version_id: uuid.UUID
    name: str
    domain: str | None = None
    similarity: float
    purpose_statement: str | None = None


class ChatTurnRead(BaseModel):
    """The response shape for every chat-builder endpoint: either the next clarifying
    question, a reuse suggestion to choose from, or the current draft state (and, once
    `status == "confirmed"`, the id of the real Scorecard materialized from it)."""

    session_id: uuid.UUID
    status: str  # "awaiting_clarification" | "awaiting_similar_choice" | "gathering" | "confirmed"
    draft: dict[str, Any]
    question: ChatClarificationQuestion | None = None
    similar_suggestions: list[ChatSimilarSuggestion] | None = None
    assistant_message: str | None = None
    materialized_scorecard_id: uuid.UUID | None = None
    # State of the chart behind this chat (see `chart_states` in app/api/v1/chat.py): a trashed chart's id is only
    # exposed to its owner (who can restore it); `deleted` = the chart this chat built no longer exists.
    chart_state: ChartState = "none"
    chart_can_restore: bool = False
    materialized_scorecard_version_id: uuid.UUID | None = None
    # Persistent-across-refresh "turn in progress" marker (see app/api/v1/chat.py /
    # ChatSession.pending_turn_started_at) — true when this session has a turn currently
    # running server-side (not yet stale per STALE_TURN_TIMEOUT_SECONDS). A page load/
    # refresh checks this via GET /chat/sessions/{id} to show "still working" instead of a
    # blank composer, and polls until it clears.
    turn_in_progress: bool = False
    # Real, AI-generated (or deterministically derived) session title — see
    # app/models/chat_session.py::ChatSession.title and app/ai/session_title.py. Set on
    # EVERY turn response (not just GET), so a brand-new session's very first, blocking
    # POST /chat/sessions response already carries it — the frontend never needs a second
    # round-trip just to learn the title it was generated with (see ChatWorkspace).
    title: str | None = None
    # Background turns (see app/api/v1/chat.py): why the LAST turn failed ("turn_error_code" is
    # "bedrock_unavailable" | "timeout" | "interrupted" | "turn_failed"); None while a turn runs
    # and after a successful one. The POST that starts a turn returns 202 right away with
    # turn_in_progress=true; clients poll GET /chat/sessions/{id} until it clears.
    turn_error: str | None = None
    turn_error_code: str | None = None
    # The OpenRouter model the user last picked in this chat (None = the admin's default / Bedrock).
    chat_model: str | None = None


class ChatSessionRead(ORMBase):
    id: uuid.UUID
    user_id: uuid.UUID
    status: ChatSessionStatus
    # Real, AI-generated (or, for a "Refine with assistant" session, deterministically
    # derived) title — see app/models/chat_session.py::ChatSession.title. Replaces the old
    # frontend-side `synthesizeTitle` truncation hack (see api-client.ts).
    title: str | None = None
    # Additive (Wave 3 integration): the frontend session list / "continue where you left
    # off" surfaces this directly instead of re-deriving it from chat_messages.
    context_summary: str | None = None
    target_scorecard_id: uuid.UUID | None
    created_at: datetime
    last_activity_at: datetime
    # See ChatTurnRead.turn_in_progress — surfaced here too so the sidebar session list can
    # badge a session whose turn is still running.
    turn_in_progress: bool = False
    chart_state: ChartState = "none"
    chart_can_restore: bool = False
    chat_model: str | None = None


class ChatMessageRead(ORMBase):
    """Additive (Wave 3 integration): flat read schema for `chat_messages` rows, used by
    the new `GET /chat/sessions/{id}/messages` endpoint so the frontend can render real
    chat history (the LangGraph-facing `ChatTurnRead` above only carries the *current*
    turn/draft state, not the persisted message log)."""

    id: uuid.UUID
    session_id: uuid.UUID
    role: ChatMessageRole
    content: str
    tool_calls: dict | list | None
    created_at: datetime
    # The model that wrote an assistant reply (None for user messages / older replies).
    model: str | None = None
    attachments: list[ChatAttachmentRead] = []


class ChatModelOption(BaseModel):
    id: str
    label: str
    vendor: str


class ChatAttachmentLimits(BaseModel):
    enabled: bool
    max_bytes: int
    max_files: int
    extensions: list[str]


class ChatConfigRead(BaseModel):
    """What the chat composer offers the current user (`GET /chat/config`): the model picker (OpenRouter only, the
    admin's enabled models and default) and file attachments (OpenRouter only)."""

    provider: Literal["bedrock", "openrouter"]
    model_picker: bool
    models: list[ChatModelOption]
    default_model: str | None
    attachments: ChatAttachmentLimits


class ChatTurnEventRead(ORMBase):
    """One granular step of the AI pipeline's live trace for the CURRENT/most recent turn
    (see `app/models/chat_turn_event.py` and `GET /chat/sessions/{id}/turn-events`) —
    `actor` is `"master"` or `"research_agent_{n}"` (see `app/ai/scorecard_builder.py`),
    `message` is the human-readable text the frontend renders directly. `round` (default 1)
    distinguishes which research round (see `MAX_RESEARCH_ROUNDS`) this event belongs to."""

    id: uuid.UUID
    session_id: uuid.UUID
    turn_started_at: datetime
    actor: str
    event_type: str
    message: str
    round: int
    created_at: datetime
