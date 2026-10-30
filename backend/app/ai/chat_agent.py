"""The chat assistant: the first step of every chat message (the `converse` node of the scorecard-builder graph).

Before this existed every message went straight into the scorecard builder, so "hello" or "what is a good KPI for
call quality?" started the multi-agent KPI research. Now the assistant answers first, on the chat model the user
picked (OpenRouter) or the provider's chat model (Bedrock), with real tool calling (`tool_choice: auto`, native tool
results) and a small step budget. Per message it either:

- **answers** in plain text (questions, explanations, small talk), after any `web_search` / `view_kpis` lookups;
- **edits** the current draft's KPIs (`edit_kpis`: applied by the builder's `update_draft` node, rubrics for new KPIs
  written on the builder's model), sets its scoring formula (applied here), **saves** it, or **asks** one chip
  question;
- **fixes rubrics** of one KPI, a set or a whole category (`review_rubrics`, `research_kpis`, `rewrite_guidelines`,
  `set_kpi_guidelines` in app/ai/kpi_rubric_tools.py): applied here, step by step, so it can check and go on;
- **hands off** to the scorecard builder (`build_scorecard`): the research + drafting pipeline, which runs on the
  builder's own model (OPENROUTER_KPI_MODEL on OpenRouter).

A chip answer to the builder's own clarifying question goes straight back to the builder with no model call, so the
builder's question-and-answer flow costs what it did before. The node's decision lands in `agent_route`, which
`route_after_converse` turns into the next node. `CHAT_AGENT=false` removes this node from the graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.ai import kpi_rubric_tools as rubric
from app.ai import scorecard_builder as sb
from app.ai.attachments import AttachmentContext
from app.ai.bedrock_client import BedrockClientProtocol, ConverseResult, ToolCall, ToolSpec
from app.ai.draft_schema import ScorecardDraft
from app.ai.providers import provider_of
from app.ai.scoring_formula import validate as validate_scoring_formula
from app.ai.turn_events import emit_turn_event
from app.ai.web_search import WebSearchClientProtocol
from app.config import get_settings

logger = logging.getLogger(__name__)

ACTOR = "assistant"
# Model calls per message (lookups + the final answer), and web searches per message (the session-wide cap is the
# builder's own MAX_WEB_SEARCH_CALLS_PER_SESSION, shared with it).
MAX_AGENT_STEPS = 12
MAX_AGENT_SEARCHES = 3
# research_kpis searches: per message, and per session (its own budget, separate from the builder's).
MAX_RUBRIC_SEARCHES = 16
MAX_RUBRIC_SEARCHES_PER_SESSION = 60
# How much of the conversation the assistant re-reads each message.
HISTORY_TURNS = 30
# Draft outline lines in the system prompt (a 200-KPI scorecard is summarised, never truncated silently).
MAX_OUTLINE_KPIS = 150

FALLBACK_ANSWER = "Sorry, I couldn't put an answer together for that. Could you rephrase it?"

# --- tools ------------------------------------------------------------------------------------------------------

BUILD_SCORECARD_TOOL = ToolSpec(
    name="build_scorecard",
    description=(
        "Hand the request to the scorecard builder: a slower pipeline that researches the domain on the web, drafts "
        "categories and KPIs with full 0-10 guidelines and weights, and asks the user to confirm. Call it ONLY when "
        "the user wants a scorecard created or substantially reworked: a new scorecard from a description, from an "
        "attached SOP / policy / KPI list, a large restructuring, or regenerating the draft; and when the user is "
        "answering a question the builder itself asked. Never call it for greetings, general questions, "
        "explanations, or small edits to existing KPIs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "brief": {
                "type": "string",
                "description": (
                    "What the builder should do, in one or two sentences, including anything decided earlier in the "
                    "conversation that the latest message relies on (e.g. 'Call-centre QA scorecard for inbound "
                    "support calls, about 20 KPIs, use the attached SOP'). Empty when the user is only answering "
                    "the builder's question."
                ),
            },
        },
        "required": ["brief"],
    },
)

WEB_SEARCH_TOOL = ToolSpec(
    name="web_search",
    description=(
        "Search the web for current, checkable information: industry benchmarks, standards, regulations, definitions, "
        "recent facts. Use focused queries. Cite what you use as Markdown links in your answer."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A focused web search query."}},
        "required": ["query"],
    },
)

VIEW_KPIS_TOOL = ToolSpec(
    name="view_kpis",
    description=(
        "Show the full details of KPIs in the current draft: weight, level, parent, scoring flag and the 0-10 "
        "guideline text with each level's threshold. The system prompt only lists names and weights, so call this "
        "before explaining or rewriting a KPI's rubric."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20,
                      "description": "Exact KPI names from the draft."},
        },
        "required": ["names"],
    },
)

EDIT_KPIS_TOOL = ToolSpec(
    name="edit_kpis",
    description=(
        "Change the current draft's existing KPIs with a SMALL list of operations: change a weight, rename, remove, "
        "add a few KPIs, move one to another category, include/exclude from scoring. The "
        "server applies them, rebalances every leaf weight to exactly 100 and writes the guidelines of added or "
        "regenerated KPIs itself, then shows the user a before/after summary. Copy KPI names exactly. To improve "
        "guidelines use rewrite_guidelines / set_kpi_guidelines instead. For a wholesale restructuring or a "
        "brand-new scorecard use build_scorecard instead."
    ),
    input_schema=sb.EDIT_KPIS_TOOL.input_schema,
)

UPDATE_SCORING_FORMULA_TOOL = ToolSpec(
    name="update_scoring_formula",
    description=(
        "Set or clear a custom formula for how the draft's final score is computed (default: the weighted average "
        "of leaf KPI scores). Reference a KPI as kpi[\"Exact KPI Name\"]; supports + - * / ** and min, max, avg, "
        "mean, sqrt, abs. Validated immediately: an invalid formula comes back with the error so you can fix it. "
        "Pass null to clear it."
    ),
    input_schema={
        "type": "object",
        "properties": {"formula": {"type": ["string", "null"], "description": "The formula, or null to clear it."}},
        "required": ["formula"],
    },
)

SAVE_SCORECARD_TOOL = ToolSpec(
    name="save_scorecard",
    description=(
        "Save the current draft as a scorecard (or as a new version of the chart this chat edits). Only when the "
        "user has said to save it. If the draft is incomplete the builder fills what is missing first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "assistant_message": {"type": "string", "description": "A short note to the user about the save."},
        },
        "required": ["assistant_message"],
    },
)

READ_ATTACHMENT_TOOL = ToolSpec(
    name="read_attachment",
    description=(
        "Read the extracted text of a file attached in this chat, from a character offset. The messages only carry "
        "the start of large files (a note says where the cut is); read on from that offset when you need the rest."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "attachment_id": {"type": "string", "description": "The file id from the list of attached files."},
            "offset": {"type": "integer", "minimum": 0, "description": "Character offset to start from (default 0)."},
        },
        "required": ["attachment_id"],
    },
)

ASK_USER_TOOL = ToolSpec(
    name="ask_user",
    description=(
        "Ask the user ONE question with 2-5 short clickable answer options, when you need a decision only they can "
        "make. Never ask several questions at once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
        "required": ["question"],
    },
)

# Tools that end the assistant's step (the graph moves on); the others are lookups it gets the result of.
_TERMINAL_TOOLS = {"build_scorecard", "edit_kpis", "save_scorecard", "ask_user"}

_SYSTEM_PROMPT = """You are the assistant of ScoreSmith, a quality scorecard system. People use it to design quality \
scorecards (weighted KPIs, each with a 0-10 guideline) and to evaluate evidence (documents, calls, videos) against them.

Answer the user's LATEST message. Decide what it needs:
- A question, explanation, comparison, opinion or small talk -> answer it directly in plain text (Markdown is \
fine). Use web_search first when current or checkable facts matter, and cite sources as Markdown links.
- A small change to the current draft's existing KPIs (weights, names, removing or adding a few KPIs, moving, \
include/exclude) -> edit_kpis. How the score is computed -> update_scoring_formula.
- Improving guidelines / rubrics / "the scoring is unsure or wrong" for one KPI, several, a category or all of them \
-> work on the rubrics directly, in as many steps as it takes: review_rubrics first (it shows how sure the \
evaluation scorer was on this chart's past evaluations, and the problems in each rubric), research_kpis when real \
benchmarks would help, then rewrite_guidelines (whole rubrics, a category or a list at a time) or \
set_kpi_guidelines (exact levels you write yourself, e.g. only levels 4-7), then review_rubrics again on what you \
changed. Good levels state an observable condition with the count / percentage / time that separates each level \
from its neighbours. Rubric changes apply to the draft; tell the user to save it to update the chart.
- "Save it" / "looks good, save" -> save_scorecard.
- A new scorecard, a scorecard built from an attached file, a large restructuring, or regenerating the draft -> \
build_scorecard with a clear brief. The builder does the research and drafting; do not draft KPIs yourself in text.
- A decision only the user can make -> ask_user (one question, short options).
Never say you changed, saved or built something unless you called the tool that does it. Be concise and specific.

{pending_question}{attached_files}Current draft in this chat:
{draft_outline}
"""


# --- prompt pieces ------------------------------------------------------------------------------------------------


def draft_outline(draft: ScorecardDraft) -> str:
    """The draft as a compact outline for the system prompt: scalar fields, then one line per KPI (indented by level,
    weight or "category", scoring flag), then completeness. Rubrics are left out (`view_kpis` returns them)."""
    if not draft.kpis and not any((draft.name, draft.purpose, draft.domain)):
        return "(nothing drafted yet in this chat)"
    lines = [
        f"Name: {draft.name or '(not set)'}",
        f"Purpose: {draft.purpose or '(not set)'}",
        f"Domain: {draft.domain or '(not set)'}",
        f"Audience: {draft.audience or '(not set)'}",
        f"Target score: {draft.target_score if draft.target_score is not None else '(not set)'}",
        f"Scoring formula: {draft.scoring_formula or 'weighted average (default)'}",
        "KPIs:",
    ]
    parents = {k.parent_name for k in draft.kpis if k.parent_name}
    for kpi in draft.kpis[:MAX_OUTLINE_KPIS]:
        indent = "  " * (kpi.level - 1)
        if kpi.name in parents:
            detail = "category"
        else:
            detail = f"{kpi.weight:g}%" if kpi.weight is not None else "no weight"
            if not kpi.included_in_scoring:
                detail += ", excluded from scoring"
        lines.append(f"{indent}- {kpi.name} ({detail})")
    if len(draft.kpis) > MAX_OUTLINE_KPIS:
        lines.append(f"... and {len(draft.kpis) - MAX_OUTLINE_KPIS} more KPIs (use view_kpis by name)")
    if not draft.kpis:
        lines.append("(none yet)")
    missing = draft.missing_fields()
    lines.append("Complete: yes" if not missing else f"Still missing: {', '.join(missing[:12])}")
    return "\n".join(lines)


def _pending_question_text(answered: dict[str, Any] | None) -> str:
    if not answered or not answered.get("question"):
        return ""
    options = [str(o) for o in answered.get("options") or [] if str(o).strip()]
    asked = f'"{answered["question"]}"' + (f" (options: {'; '.join(options)})" if options else "")
    if answered.get("from") == "assistant":
        return f"You just asked the user {asked}; the latest message is their answer.\n\n"
    return (
        f"The scorecard builder just asked the user {asked}. If the latest message answers that question, call "
        "build_scorecard so the builder continues.\n\n"
    )


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_chip_answer(message: str, answered: dict[str, Any] | None) -> bool:
    """True when `message` is exactly one of the options the BUILDER offered (a chip click on its question)."""
    if not answered or answered.get("from") == "assistant":
        return False
    target = _normalise(message)
    return bool(target) and any(_normalise(str(o)) == target for o in answered.get("options") or [])


# --- tool execution -----------------------------------------------------------------------------------------------


def _view_kpis(draft: ScorecardDraft, names: Any) -> str:
    wanted = [str(n) for n in names] if isinstance(names, list) else []
    by_name = {k.name.lower(): k for k in draft.kpis}
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in wanted[:20]:
        kpi = by_name.get(name.strip().lower())
        if kpi is None:
            missing.append(name)
            continue
        found.append(
            {
                "name": kpi.name,
                "level": kpi.level,
                "parent": kpi.parent_name,
                "weight": kpi.weight,
                "included_in_scoring": kpi.included_in_scoring,
                "guidelines": {
                    level: rubric.view_line(g)
                    for level, g in sorted(kpi.guidelines.items(), key=lambda item: int(item[0]))
                },
            }
        )
    out = json.dumps(found, ensure_ascii=False)
    if missing:
        out += f"\nNot in the draft: {', '.join(missing)}"
    return out


def _set_formula(draft: ScorecardDraft, formula: Any) -> tuple[ScorecardDraft | None, str]:
    """Validate and apply a scoring formula. Returns (new draft or None when rejected, tool-result text)."""
    formula = formula if (formula is None or isinstance(formula, str)) else str(formula)
    if formula is not None and not formula.strip():
        formula = None
    result = validate_scoring_formula(formula, [k.name for k in draft.kpis])
    if not result.valid:
        return None, f"REJECTED (invalid formula): {result.error}"
    note = f"Scoring formula set: {formula!r}" if formula else "Scoring formula cleared: the default weighted average."
    if result.unused_kpis:
        note += f" (KPIs not referenced: {', '.join(result.unused_kpis)})"
    return draft.model_copy(update={"scoring_formula": formula}), note


def _tool_result_block(call_id: str, text: str) -> dict[str, Any]:
    return {"toolResult": {"toolUseId": call_id, "content": [{"text": text}]}}


def _assistant_blocks(result: ConverseResult, calls: list[ToolCall]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if result.text:
        blocks.append({"text": result.text})
    blocks.extend({"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.input}} for c in calls)
    if result.reasoning_details:
        blocks.append({"reasoningDetails": result.reasoning_details})
    return blocks


def _calls_of(result: ConverseResult) -> list[ToolCall]:
    if result.tool_calls:
        return list(result.tool_calls)
    if result.tool_name:
        return [ToolCall(id=result.tool_use_id or "call_0", name=result.tool_name, input=result.tool_input or {})]
    return []


def _history(
    messages: list[sb.ChatTurn], cache: bool, native_blocks: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """The recent conversation in Converse shape, starting with a user turn (Converse requires it). `native_blocks`
    (images / scanned PDFs sent with the latest message) are added to the latest user message. With `cache`, a cache
    point after the latest user message lets the next steps of this message (and the next message, within the cache
    lifetime) re-read the shared prefix cheaply."""
    converse = sb._messages_to_converse(list(messages[-HISTORY_TURNS:]))
    while converse and converse[0]["role"] != "user":
        converse.pop(0)
    if not converse:
        converse = [{"role": "user", "content": [{"text": "(empty message)"}]}]
    if converse[-1]["role"] == "user":
        extra = list(native_blocks or []) + ([{"cachePoint": {"type": "default"}}] if cache else [])
        if extra:
            converse[-1] = {**converse[-1], "content": [*converse[-1]["content"], *extra]}
    return converse


def _attached_files_text(attachments: AttachmentContext | None) -> str:
    if attachments is None or not attachments.files:
        return ""
    return (
        "Files attached in this chat (their text is inside the user messages, cut to fit when large; "
        "read_attachment reads on from an offset):\n" + attachments.summary() + "\n\n"
    )


# --- the node -----------------------------------------------------------------------------------------------------


async def converse(state: sb.BuilderState, config: RunnableConfig) -> dict[str, Any]:
    """The `converse` graph node; see the module docstring."""
    configurable = config.get("configurable", {})
    client: BedrockClientProtocol = configurable["bedrock_client"]
    builder_model_id: str | None = configurable.get("chat_model_id")
    model_id: str | None = configurable.get("conversation_model_id") or builder_model_id
    web_search_client: WebSearchClientProtocol | None = configurable.get("web_search_client")
    attachments: AttachmentContext | None = configurable.get("attachments")
    session_id = state["session_id"]
    turn_started_at: datetime | None = configurable.get("turn_started_at")

    messages = state.get("messages") or []
    answered = state.get("answered_question")
    last_user = sb._last_user_message(messages)

    if _is_chip_answer(last_user, answered):
        # A chip click on the builder's own question: the builder continues, no assistant model call.
        return {"agent_route": "build", "build_brief": "", "answered_question": None}

    draft = ScorecardDraft.model_validate(state["draft"])
    openrouter = provider_of(client) == "openrouter"
    settings = get_settings()
    searches_before = int(state.get("web_search_calls_used") or 0)
    searches = 0

    system = _SYSTEM_PROMPT.format(
        pending_question=_pending_question_text(answered),
        attached_files=_attached_files_text(attachments),
        draft_outline=draft_outline(draft),
    )
    has_kpis = bool(draft.kpis)
    tools: list[ToolSpec] = [BUILD_SCORECARD_TOOL, ASK_USER_TOOL]
    if has_kpis:
        tools += [VIEW_KPIS_TOOL, EDIT_KPIS_TOOL, UPDATE_SCORING_FORMULA_TOOL, SAVE_SCORECARD_TOOL]
        tools += [t for t in rubric.TOOLS if t.name != "research_kpis" or web_search_client is not None]
    rubric_session = rubric.RubricSession()
    rubric_searches_before = int(state.get("rubric_searches_used") or 0)
    rubric_searches = 0
    if web_search_client is not None:
        tools.append(WEB_SEARCH_TOOL)
    if attachments is not None and attachments.files:
        tools.append(READ_ATTACHMENT_TOOL)

    native = attachments.native_blocks if (attachments is not None and openrouter) else None
    converse_messages = _history(messages, cache=openrouter, native_blocks=native)
    base: dict[str, Any] = {"answered_question": None}
    await emit_turn_event(session_id, turn_started_at, ACTOR, "thinking", "Reading your message…")

    for step in range(MAX_AGENT_STEPS):
        last_step = step == MAX_AGENT_STEPS - 1
        step_system = system + (
            "\n\nYou have used your lookups for this message: answer now in plain text." if last_step else ""
        )
        result = await asyncio.to_thread(
            client.converse,
            messages=converse_messages,
            system=step_system,
            tools=tools,
            force_tool_use=False,
            model_id=model_id,
            reasoning_effort=settings.openrouter_chat_reasoning if openrouter else None,
            keep_reasoning=openrouter,
            session_key=str(session_id) if openrouter else None,
        )
        usage = result.usage or {}
        logger.info(
            "chat agent step %d model=%s prompt_tokens=%s completion_tokens=%s cost=%s",
            step + 1, model_id, usage.get("prompt_tokens") or usage.get("inputTokens"),
            usage.get("completion_tokens") or usage.get("outputTokens"), usage.get("cost"),
        )
        calls = _calls_of(result)
        if result.truncated and not calls and not result.text:
            break
        if not calls:
            await emit_turn_event(session_id, turn_started_at, ACTOR, "completed", "Answered.")
            return {**base, **_answer(result.text, draft, state, searches_before + searches),
                    "rubric_searches_used": rubric_searches_before + rubric_searches}

        terminal = next((c for c in calls if c.name in _TERMINAL_TOOLS), None)
        if terminal is not None:
            update = await _terminal(terminal, result, draft, state, client, builder_model_id, configurable)
            return {**base, "draft": draft.model_dump(mode="json"),
                    "web_search_calls_used": searches_before + searches,
                    "rubric_searches_used": rubric_searches_before + rubric_searches, **update}
        if last_step:
            break

        tool_results: list[dict[str, Any]] = []
        for call in calls:
            if call.name == "web_search":
                query = str(call.input.get("query") or "").strip()
                budget_left = (
                    searches < MAX_AGENT_SEARCHES
                    and searches_before + searches < sb.MAX_WEB_SEARCH_CALLS_PER_SESSION
                )
                if web_search_client is None or not query or not budget_left:
                    text = "web_search is not available for the rest of this message: answer with what you have."
                else:
                    searches += 1
                    await emit_turn_event(session_id, turn_started_at, ACTOR, "searching", f'Searching: "{query}"')
                    try:
                        found = await web_search_client.search(query)
                    except Exception:  # noqa: BLE001 - search() never raises by contract; a surprise still can't
                        logger.warning("chat agent web_search raised; treating as no results.", exc_info=True)
                        found = []
                    await emit_turn_event(
                        session_id, turn_started_at, ACTOR, "search_result",
                        f'Found {len(found)} result(s) for "{query}"',
                    )
                    text = sb._format_search_results(query, found)
            elif call.name == "read_attachment" and attachments is not None:
                await emit_turn_event(session_id, turn_started_at, ACTOR, "reading", "Reading an attached file…")
                text = attachments.read(str(call.input.get("attachment_id") or ""), call.input.get("offset") or 0)
            elif call.name == "view_kpis":
                await emit_turn_event(session_id, turn_started_at, ACTOR, "reading", "Looking at KPI details…")
                text = _view_kpis(draft, call.input.get("names"))
            elif call.name in rubric.TOOL_NAMES:
                draft, text, used = await _rubric_tool(
                    call, draft, state, client, builder_model_id, web_search_client, rubric_session,
                    budget=min(
                        MAX_RUBRIC_SEARCHES - rubric_searches,
                        MAX_RUBRIC_SEARCHES_PER_SESSION - rubric_searches_before - rubric_searches,
                    ),
                    turn_started_at=turn_started_at,
                )
                rubric_searches += used
            elif call.name == "update_scoring_formula":
                new_draft, text = _set_formula(draft, call.input.get("formula"))
                if new_draft is not None:
                    draft = new_draft
                    await emit_turn_event(session_id, turn_started_at, ACTOR, "formula", text)
            else:
                text = f"Unknown tool {call.name!r}."
            tool_results.append(_tool_result_block(call.id, text))
        converse_messages.append({"role": "assistant", "content": _assistant_blocks(result, calls)})
        converse_messages.append({"role": "user", "content": tool_results})

    # Step budget used up (or an empty truncated reply): answer with what the model said last, if anything.
    logger.warning("chat agent ended without a final answer (session=%s).", session_id)
    await emit_turn_event(session_id, turn_started_at, ACTOR, "completed", "Answered.")
    return {**base, **_answer(None, draft, state, searches_before + searches),
            "rubric_searches_used": rubric_searches_before + rubric_searches}


async def _rubric_tool(
    call: ToolCall,
    draft: ScorecardDraft,
    state: sb.BuilderState,
    client: BedrockClientProtocol,
    builder_model_id: str | None,
    web_search_client: WebSearchClientProtocol | None,
    rs: rubric.RubricSession,
    *,
    budget: int,
    turn_started_at: datetime | None,
) -> tuple[ScorecardDraft, str, int]:
    """Runs one rubric tool (app/ai/kpi_rubric_tools.py). Returns the draft (changed by rewrite / set), the tool
    result and the number of web searches made."""
    session_id = state["session_id"]
    args = call.input or {}
    if rs.health is None and call.name in ("review_rubrics", "rewrite_guidelines"):
        rs.health = await rubric.scoring_health(await rubric.target_scorecard_id(session_id))
    if call.name == "review_rubrics":
        await emit_turn_event(session_id, turn_started_at, ACTOR, "reading", "Reviewing the KPI guidelines…")
        return draft, rubric.review_text(draft, args, rs.health or {}), 0
    if call.name == "research_kpis":
        if web_search_client is None or budget <= 0:
            return draft, "research_kpis is not available for the rest of this message: use what you have.", 0
        count = len(args.get("kpis") or [])
        await emit_turn_event(
            session_id, turn_started_at, ACTOR, "searching", f"Researching {min(count, budget)} KPI(s)…"
        )
        text, used = await rubric.research(draft, args, web_search_client, rs, budget)
        return draft, text, used
    if call.name == "rewrite_guidelines":
        await emit_turn_event(session_id, turn_started_at, ACTOR, "guidelines", "Rewriting KPI guidelines…")
        new_draft, text = await rubric.rewrite(
            draft, args, rs, client, builder_model_id, sb._conversation_context_text(state.get("messages") or [])
        )
        if new_draft is not draft:
            await emit_turn_event(session_id, turn_started_at, ACTOR, "guidelines", text.splitlines()[0])
        return new_draft, text, 0
    # set_kpi_guidelines
    new_draft, text = rubric.set_levels(draft, args)
    if new_draft is None:
        return draft, text, 0
    await emit_turn_event(session_id, turn_started_at, ACTOR, "guidelines", text.splitlines()[0])
    return new_draft, text, 0


def _answer(text: str | None, draft: ScorecardDraft, state: sb.BuilderState, searches_used: int) -> dict[str, Any]:
    reply = sb._strip_system_prefix((text or "").strip()) or FALLBACK_ANSWER
    return {
        "agent_route": "answer",
        "draft": draft.model_dump(mode="json"),
        "web_search_calls_used": searches_used,
        "pending_tool": {"name": "respond_conversationally", "input": {"response": reply}},
        "messages": [{"role": "assistant", "content": reply}],
    }


async def _terminal(
    call: ToolCall,
    result: ConverseResult,
    draft: ScorecardDraft,
    state: sb.BuilderState,
    client: BedrockClientProtocol,
    builder_model_id: str | None,
    configurable: dict[str, Any],
) -> dict[str, Any]:
    """The graph update for a tool that ends the assistant's step."""
    session_id = state["session_id"]
    turn_started_at = configurable.get("turn_started_at")
    args = call.input or {}
    if call.name == "build_scorecard":
        brief = str(args.get("brief") or "").strip()
        await emit_turn_event(
            session_id, turn_started_at, ACTOR, "completed", "Handing over to the scorecard builder…"
        )
        update: dict[str, Any] = {"agent_route": "build", "build_brief": brief, "llm_turn_count": 0}
        if brief:
            # Seen by the builder's model as a system note (never shown to the user: internal notes are filtered).
            update["messages"] = [{"role": "tool", "content": f"[handoff from the chat assistant] {brief}"}]
        return update
    if call.name == "edit_kpis":
        await emit_turn_event(session_id, turn_started_at, ACTOR, "completed", "Editing the KPIs…")
        prepared = await sb._write_edit_rubrics(
            dict(args), draft, client, builder_model_id,
            sb._conversation_context_text(state.get("messages") or []), session_id, turn_started_at, ACTOR,
        )
        return {
            "agent_route": "edit",
            "pending_tool": {"name": "edit_kpis", "input": prepared},
            # The edit summary's "before" values (see update_draft).
            "turn_base_weights": {k.name: k.weight for k in draft.kpis},
        }
    if call.name == "save_scorecard":
        await emit_turn_event(session_id, turn_started_at, ACTOR, "completed", "Saving the scorecard…")
        note = str(args.get("assistant_message") or result.text or "").strip()
        return {
            "agent_route": "save",
            "pending_tool": {
                "name": "update_draft",
                "input": {"patch": {}, "confirmed": True, "assistant_message": note},
            },
        }
    # ask_user
    question = str(args.get("question") or "").strip() or "Could you tell me a bit more?"
    options = [str(o).strip() for o in args.get("options") or [] if str(o).strip()][:5]
    await emit_turn_event(session_id, turn_started_at, ACTOR, "completed", f"Asking: {question}")
    return {
        "agent_route": "ask",
        "question_from": "assistant",
        "pending_tool": {
            "name": "ask_clarification",
            "input": {"question": question, "options": options, "missing_fields": []},
        },
        "messages": [{"role": "assistant", "content": question}],
    }


def route_after_converse(state: sb.BuilderState) -> str:
    return {
        "build": "check_similarity",
        "edit": "update_draft",
        "save": "update_draft",
        "ask": "ask_clarification",
    }.get(str(state.get("agent_route") or ""), "respond_conversationally")
