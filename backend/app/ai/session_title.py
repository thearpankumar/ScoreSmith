"""Real, AI-generated chat-session titles (Issue 1 of this pass — see task notes).

Previously `chat_sessions` had no `title` concept at all: the frontend faked one
client-side by truncating the user's raw first message (see
`frontend/lib/api-client.ts::synthesizeTitle`, removed by this pass). This module
generates a genuine short, descriptive title via ONE fast/cheap Bedrock call — deliberately
`settings.bedrock_judge_model_id` (Z.ai GLM-4.7-flash), the same lighter/faster model
`app/ai/judge.py` already uses, NOT the heavier chat-builder model
(`settings.bedrock_chat_model_id`, GLM-5) — so this adds minimal latency ahead of the much
heavier LangGraph turn (`check_similarity` / `research_kpis` / `propose_kpis` — see
`app/ai/scorecard_builder.py`) that follows it.

Deliberately NOT a LangGraph node: it must run exactly once, as the very first thing when
a brand-new session's first message arrives, before that heavier graph work starts (see
`app/api/v1/chat.py::start_chat_session`) — a plain function call there, not a graph node,
keeps it decoupled from the graph's own checkpointing/tool-call machinery (which is built
around structured `update_draft`-style tool calls, not a one-off plain-text title) and
keeps every existing scorecard-builder-graph test (which scripts an exact, order-sensitive
sequence of `.converse()` calls against `FakeBedrockClient` — see `tests/fakes.py`)
completely unaffected, since none of those tests go through the HTTP API layer this lives
in.

Contract: NEVER raises (mirrors `web_search.py`/`emit_turn_event`'s own "best-effort,
never break the turn" contracts elsewhere in this codebase) — a failed or malformed title
call simply returns `None`, and the caller leaves `chat_sessions.title` unset rather than
failing session creation over a cosmetic feature.
"""

from __future__ import annotations

import logging
import re

from app.ai.bedrock_client import BedrockClientProtocol

logger = logging.getLogger(__name__)

# Generous but bounded — a title is meant to be 3-6 words; this is just a hard safety cap
# against a model that ignores the instruction and rambles.
MAX_TITLE_LENGTH = 80

_TITLE_SYSTEM_PROMPT = """You generate a short, descriptive title for a chat session where \
a user is describing a quality scorecard they want to build (KPIs, weights, guidelines).

Reply with ONLY the title itself — 3 to 6 words, title case, no quotation marks, no \
trailing punctuation, no preamble like "Title:" or "Here is a title". Base it on the \
SPECIFIC subject matter the user described (e.g. "Vendor Compliance Review Scorecard", \
"Support Ticket Response Quality", "Vendor Security Audit KPIs") — never a generic phrase \
like "New Scorecard" or "Quality Scorecard" unless the user's message truly gives you \
nothing more specific to go on."""


def _clean_title(raw: str) -> str | None:
    """Strips wrapping quotes/whitespace/a stray "Title:" prefix a model might still add
    despite the instruction, and enforces MAX_TITLE_LENGTH. Returns None for anything that
    cleans down to nothing usable."""
    text = raw.strip()
    text = re.sub(r'^(title|chat title)\s*[:\-]\s*', "", text, flags=re.IGNORECASE)
    text = text.strip().strip('"').strip("'").strip()
    # A model occasionally still wraps the whole reply in a sentence — take just the first
    # line, which is where the actual title lives in every observed case.
    text = text.splitlines()[0].strip() if text else text
    if not text:
        return None
    if len(text) > MAX_TITLE_LENGTH:
        text = text[:MAX_TITLE_LENGTH].rstrip()
    return text


def generate_session_title(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    first_message: str,
) -> str | None:
    """Synchronous (matches `BedrockClientProtocol.converse`'s own sync contract — run via
    `asyncio.to_thread` by the caller, exactly like every other Bedrock call site in this
    codebase). One plain-text (non-tool-use) Converse call — deliberately not
    `force_tool_use`d, since a title is free text, not a structured object; also keeps this
    call trivially distinguishable from every scripted tool-use response in the test
    suite, so it can never be mistaken for a graph tool call even if a test's fake happens
    to share the same client instance.

    Returns None (never raises) on any failure — including an empty/whitespace
    `first_message`, which nothing meaningful can be titled from."""
    text = (first_message or "").strip()
    if not text:
        return None
    try:
        result = bedrock.converse(
            messages=[{"role": "user", "content": [{"text": text}]}],
            system=_TITLE_SYSTEM_PROMPT,
            model_id=model_id,
        )
    except Exception:  # noqa: BLE001 — best-effort; see module docstring
        logger.warning("generate_session_title: Bedrock call failed; leaving title unset.", exc_info=True)
        return None

    if not result.text:
        logger.warning(
            "generate_session_title: model returned no text (stop_reason=%r); leaving title unset.",
            getattr(result, "stop_reason", None),
        )
        return None

    title = _clean_title(result.text)
    if title is None:
        logger.warning("generate_session_title: model reply cleaned down to nothing usable; leaving title unset.")
    return title
