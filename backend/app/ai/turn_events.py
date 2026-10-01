"""Best-effort persisted event writer for the live chat-turn trace UI (replaces the old
generic "Assistant is thinking…" indicator — see `app/models/chat_turn_event.py` for the
table this writes to, and `scorecard_builder.py`'s `research_kpis`/`_run_research_agent`/
`propose_kpis` for the call sites that actually emit events as the pipeline runs).

`emit_turn_event` opens its OWN short-lived `AsyncSession` per call rather than reusing the
request-scoped session threaded through LangGraph's `config.configurable["db_session"]`.
This matters specifically because of the multi-agent, per-category research fan-out: up to
`MAX_CATEGORIES` `_run_research_agent` invocations (one per decided KPI category) run truly
concurrently via
`asyncio.gather`, and a single `AsyncSession` is NOT safe to use from multiple coroutines
at once (SQLAlchemy raises `InvalidRequestError` on interleaved use of one session across
concurrent tasks). A fresh session per event write, drawn from the existing app-wide
connection pool (`app.db.AsyncSessionLocal`), sidesteps that entirely at the cost of one
extra short-lived connection checkout per event — cheap, and never on the critical path
that blocks the graph (see the "never raise" contract below).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)


async def emit_turn_event(
    session_id: str,
    turn_started_at: datetime | None,
    actor: str,
    event_type: str,
    message: str,
    *,
    round: int = 1,
) -> None:
    """Persist one turn-trace event. NEVER raises — a failure to write a UX-only trace
    event must never break the actual chat turn it's describing (mirrors the "never raise"
    contracts already established by `web_search.py` and `_run_research_agent` itself).

    `turn_started_at=None` is a valid, silent no-op: some callers (`seed_session`, this
    module's own tests exercising graph logic in isolation with no HTTP request/turn
    marker) genuinely have no turn to correlate events to.

    `round` (default 1, keyword-only) distinguishes which research round this event
    belongs to (see `MAX_RESEARCH_ROUNDS`/`research_kpis` in `scorecard_builder.py`) —
    every call site outside the multi-round research loop simply omits it and gets the
    correct default, so this is a purely additive parameter.
    """
    if turn_started_at is None:
        return
    try:
        # Imported lazily so importing this module never has the side effect of creating
        # the async engine/pool (matters for any pure-unit test that imports
        # scorecard_builder without a real DATABASE_URL configured).
        from app.db import AsyncSessionLocal
        from app.models.chat_turn_event import ChatTurnEvent

        async with AsyncSessionLocal() as db:
            db.add(
                ChatTurnEvent(
                    session_id=uuid.UUID(str(session_id)),
                    turn_started_at=turn_started_at,
                    actor=actor,
                    event_type=event_type,
                    message=message,
                    round=round,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — see module/function docstring: never break the turn
        logger.warning(
            "emit_turn_event failed (session_id=%s actor=%r event_type=%r); continuing.",
            session_id,
            actor,
            event_type,
            exc_info=True,
        )
