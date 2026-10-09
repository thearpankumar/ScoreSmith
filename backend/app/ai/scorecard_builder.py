"""LangGraph state machine for the chat-driven scorecard builder.

Implements the plan's "stateful ask clarifying questions without losing progress" design:
a `ScorecardDraft` (see `draft_schema.py`) is carried in LangGraph state and checkpointed
after every node via `AsyncPostgresSaver`, pointed at the same `DATABASE_URL` as the rest
of the app. `interrupt()` pauses the graph at the clarification point so state survives a
server restart mid-question — proven in this build against a real Postgres instance by
literally closing one `AsyncPostgresSaver` connection and resuming with a brand new one
(see `tests/test_scorecard_builder.py::test_state_survives_simulated_restart`).

**Thread id choice**: the LangGraph `thread_id` is `str(chat_sessions.id)` directly — no
extra mapping column. `chat_sessions` already exists as the "one session, one id" concept
in the CRUD schema, and LangGraph's checkpointer keys are opaque strings, so reusing the
CRUD primary key is the simplest option that satisfies "client only needs to hold
session_id" from the plan. The `chat_messages` table is written to *in addition* to
LangGraph's own checkpoint tables (`checkpoints`/`checkpoint_writes`/`checkpoint_blobs`,
created by `AsyncPostgresSaver.setup()` — managed by langgraph itself, not Alembic) by the
API layer (`app/api/v1/chat.py`), so the existing relational chat history stays populated
for any non-LangGraph consumer (e.g. a future "show me the conversation" admin view)
without the API layer needing to know LangGraph's internal checkpoint format.

**Graph shape** (nodes exactly as named in the plan: `gather_info` -> `propose_kpis` ->
`ask_clarification` <-> `update_draft` -> `confirm`, with `check_similarity`/
`suggest_similar` and `research_kpis` as additive nodes inserted ahead of `propose_kpis` —
see `research_kpis`'s own docstring for the multi-agent research fan-out it runs, once per
session, immediately before the first `propose_kpis` call): `propose_kpis` is the one node
that calls the LLM to actually shape the draft, and — since GLM-5 must emit exactly one
structured tool call per turn per
the plan — every turn resolves to either `ask_clarification` (pauses on `interrupt()`,
then loops back to `propose_kpis` once answered) or `update_draft` (validates the patch,
then loops back to `propose_kpis` unless the model has both patched a complete draft *and*
explicitly confirmed it, in which case `confirm` is entered and the graph ends).
`propose_kpis` therefore acts as the router the plan's "ask_clarification <-> update_draft"
arrow implies — LangGraph routes through explicit conditional edges out of a decision
node, so a raw bidirectional edge between two leaf nodes isn't expressible directly; this
is the natural, faithful encoding of that arrow. "LLM first, human second" (the framework's
own stated principle) is honored because `propose_kpis` is reachable, and will call
`update_draft` with best-guess KPI candidates, before any `ask_clarification` turn happens
on a fresh draft — the system prompt explicitly instructs this ordering.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import math
import re
import sys
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.ai.bedrock_client import (
    BedrockClientProtocol,
    BedrockTimeoutError,
    BedrockUnavailableError,
    ConverseResult,
    ToolSpec,
)
from app.ai.draft_schema import MAX_HIERARCHY_LEVEL, KpiDraft, ScorecardDraft
from app.ai.jev_client import QUALITY_GATE_THRESHOLD, JevClientProtocol, QualityGateResult, quality_gate
from app.ai.request_routing import (
    extract_kpi_target,
    parse_structured_kpi_list,
    plausibly_contains_kpi_list,
    route_request,
)
from app.ai.scoring_formula import validate as validate_scoring_formula
from app.ai.similarity import find_similar_scorecards
from app.ai.turn_events import emit_turn_event
from app.ai.web_search import SearchResult, WebSearchClientProtocol
from app.config import get_settings
from app.limits.redis_semaphore import get_semaphore

if sys.platform == "win32":
    # Same rationale as app/main.py: psycopg's async mode needs the selector event loop
    # on Windows. Idempotent — set_event_loop_policy is safe to call more than once.
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger(__name__)


# --- Tool schemas (strict tool-use; see bedrock_client.py module docstring) -----------

ASK_CLARIFICATION_TOOL = ToolSpec(
    name="ask_clarification",
    description=(
        "Ask the user exactly ONE clarifying question needed to complete the scorecard "
        "draft. Prefer short chip-style options (2-5) over open prose per the product's "
        "chat UX; use an empty options array only for genuinely free-text answers "
        "(e.g. a name). If you have MORE THAN ONE thing you'd like to ask, do not combine "
        "them here (`question` is a single string, not a list) and do NOT ask the rest as "
        "a numbered list in `update_draft`'s `assistant_message` or in "
        "`respond_conversationally` either — pick the single most important/blocking one "
        "for THIS turn's `ask_clarification` call and hold the others for follow-up turns "
        "once the user has answered this one. A numbered/bulleted list of questions in "
        "plain text is exactly the broken UX (a wall of prose instead of clickable chips) "
        "this tool exists to prevent — every question the user needs to answer must reach "
        "them through this tool, one at a time, never through prose in any other tool."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The single question to ask."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "0-5 short suggested-answer chips; empty for free text.",
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Which ScorecardDraft field(s) this question targets, e.g. "
                    "['purpose'] or ['kpis[Accuracy].weight']."
                ),
            },
        },
        "required": ["question", "missing_fields"],
    },
)

UPDATE_DRAFT_TOOL = ToolSpec(
    name="update_draft",
    description=(
        "Update the ScorecardDraft. ALWAYS propose your best-guess candidate KPIs via "
        "this tool BEFORE asking a clarifying question (LLM first, human second) — do "
        "not call ask_clarification on a completely empty draft. Each key you INCLUDE in "
        "`patch` replaces that ENTIRE field wholesale (e.g. including `kpis` replaces the "
        "FULL current KPI list with exactly what you send, not a diff/merge of the two — "
        "so if you include `kpis` at all, you MUST include every KPI you want kept, not "
        "just the ones you're changing). CRITICAL, and the #1 way to avoid silently "
        "losing KPIs: any key you OMIT from `patch` is left COMPLETELY UNCHANGED — if the "
        "draft already has KPIs (e.g. merged in from research before your first turn) and "
        "you only need to set/fix name/purpose/domain/audience/target_score, OMIT `kpis` "
        "from `patch` ENTIRELY (do not restate/retype it) and only that scalar field "
        "changes; the existing KPI set is preserved exactly as-is, in full, automatically. "
        "NEVER re-type a large existing KPI list from memory just to change an unrelated "
        "field — that risks truncating/losing KPIs and wastes effort; omit the key "
        "instead. Once the draft is already complete and nothing about it is actually "
        "changing, do NOT call update_draft again with an empty or unchanged patch just "
        "to 'do something' this turn — either make a genuine change, call it once with "
        "`confirmed: true` because the user just told you to save it, or call "
        "ask_clarification instead (e.g. to check whether they want to save it as-is or "
        "adjust something) — never spend a turn re-sending data that isn't new."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "patch": {
                "type": "object",
                "description": (
                    "Top-level ScorecardDraft fields to set: name (str), purpose (str), "
                    "domain (str), audience (str), target_score (0-10 number), kpis "
                    "(full array of {name, weight (0-100), level (1-4), parent_name "
                    "(str|null), included_in_scoring (bool, default true — set false ONLY "
                    "if the user explicitly wants this KPI tracked/scored but excluded "
                    "from the weighted-score calculation and its sibling weight-sum-to-"
                    "100 rule), guidelines: {\"0\".. \"10\": {qualitative_text (str), "
                    "quantitative_criteria}}}). IMPORTANT: `quantitative_criteria` MUST "
                    "be a JSON OBJECT (dict), e.g. {\"metric\": \"MTTD\", \"target\": "
                    "\"under 5 minutes\", \"notes\": \"...\"} — free-form keys are fine, "
                    "but it can NEVER be a plain string; a bare sentence there will be "
                    "REJECTED by validation and waste a turn. Omit it (or use null) for a "
                    "rung with no quantitative benchmark rather than writing prose into it. "
                    "Pass an empty object `{}` here (not an omitted `patch`, since `patch` "
                    "is required) when confirming an already-complete draft with nothing "
                    "left to change. CATEGORIES: when it's useful for the domain, organize "
                    "`kpis` into named categories (e.g. 'Schedule', 'Budget', 'Quality') — "
                    "a category is a `level=1` KPI with `parent_name=null` and NO weight of "
                    "its own (omit/null `weight` — categories are purely organizational, no "
                    "guidelines either), and each KPI belonging to it is a `level=2` KPI "
                    "with `parent_name` set to that category's exact name AND its own "
                    "weight. Only LEAF KPIs (ones nothing else is nested under) ever carry "
                    "a weight — every leaf's weight must sum to 100 across the WHOLE "
                    "scorecard (not per category) — the sibling-weight-sum-to-100 rule now "
                    "applies globally to every leaf, never to a category."
                ),
            },
            "user_specified_kpis": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact names of KPIs the USER themselves supplied in a message (an explicit list "
                    "they pasted/typed) that this patch includes — the server pins them so no later "
                    "patch can drop/rename/re-parent them by accident. Only names the user actually "
                    "wrote; never KPIs you invented. Omit when the user supplied no KPI list."
                ),
            },
            "user_requested_kpi_changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "ORIGINAL names of user-specified (pinned) KPIs that the user's latest message "
                    "EXPLICITLY asked to remove, rename, move, reweight or rewrite, and which this "
                    "patch therefore changes. Without this, a patch whose `kpis` drops/renames/"
                    "re-parents a pinned KPI, or changes a weight/guideline the USER supplied, is "
                    "REJECTED. This is only a claim: the server verifies it against the user's actual "
                    "latest message (it must name the KPI and ask for a change) and ignores it "
                    "otherwise — if unsure, ask via ask_clarification first. Never use it on your own "
                    "initiative."
                ),
            },
            "confirmed": {
                "type": "boolean",
                "description": (
                    "true ONLY when the draft is complete and ready to save as-is. "
                    "Never set true in the same call that first completes the draft "
                    "without the user having said something confirming it. Once the user "
                    "HAS said something confirming it (e.g. 'looks good, save it'), set "
                    "this true immediately — do not make them ask twice, and do not "
                    "spend a turn re-sending the unchanged draft with confirmed left false."
                ),
            },
            "assistant_message": {
                "type": "string",
                "description": (
                    "The natural-language message shown to the user in the chat transcript "
                    "for THIS turn — write it exactly like you would a respond_conversationally "
                    "reply (e.g. \"I've drafted a comprehensive milestone quality scorecard "
                    "with 20 KPIs across Schedule, Budget, and Quality...\"), describing what "
                    "you just set/changed and why, in full. REQUIRED: this is the ONLY way "
                    "the user sees any explanation of a draft change — there is no separate "
                    "follow-up message, so do not shortchange this expecting to 'explain more' "
                    "in a later respond_conversationally call; say everything here. Never "
                    "describe specific KPI names/content/structure here (or anywhere) that "
                    "you have NOT actually included in `patch` this turn or in an earlier "
                    "turn's patch — this field narrates the patch you are actually applying, "
                    "it never substitutes for applying it. This field is for EXPLAINING the "
                    "change, not for asking the user something — never end it with a "
                    "numbered/bulleted list of follow-up questions (e.g. '1. ... 2. ... 3. "
                    "...'); a single short prompt like 'Let me know if you'd like to adjust "
                    "anything' is fine, but any question whose answer you actually need "
                    "belongs in a SEPARATE `ask_clarification` call (one question, with "
                    "chip options) on this or a later turn, never listed here as prose."
                ),
            },
        },
        "required": ["patch", "assistant_message"],
    },
)

WEB_SEARCH_TOOL = ToolSpec(
    name="web_search",
    description=(
        "Search the web for real, current industry KPIs, published standards, and "
        "measurable benchmark thresholds relevant to the domain the user described. Use "
        "this to GROUND your proposed KPIs and — especially — your quantitative "
        "guideline thresholds in real, checkable figures (e.g. a real response-time "
        "benchmark, a real compliance threshold, a real published industry standard) "
        "instead of inventing plausible-sounding numbers. Prefer calling this at least "
        "once before proposing quantitative_criteria for a domain you have not already "
        "researched earlier in this conversation. You have a limited search budget this "
        "turn — use focused queries (e.g. 'incident postmortem quality KPI industry "
        "benchmark' rather than something vague)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A focused web search query."},
        },
        "required": ["query"],
    },
)

UPDATE_SCORING_FORMULA_TOOL = ToolSpec(
    name="update_scoring_formula",
    description=(
        "Set or clear a CUSTOM scoring formula for how this scorecard's final score is "
        "computed, replacing the default weighted-average behavior. Use this when the "
        "user asks to change HOW the score is computed (e.g. 'weight compliance more "
        "heavily', 'use the minimum of these two KPIs instead of an average') — never "
        "for anything else (KPI content/weights/guidelines still go through "
        "update_draft). Reference a KPI's score as kpi[\"Exact KPI Name\"] (must match a "
        "KPI name currently in the draft exactly). Supports + - * / ** (use ** for "
        "exponentiation, NOT ^) and the functions min, max, avg, mean, sqrt, abs. "
        "Validated server-side before being accepted — an invalid formula is REJECTED "
        "with a specific error and does not change the draft; fix it and call this again."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "formula": {
                "type": ["string", "null"],
                "description": (
                    "The new formula expression, e.g. "
                    '\'min(kpi["Compliance"], kpi["Security Review"]) * 0.6 + '
                    'kpi["Docs Quality"] * 0.4\'. Pass null to clear any custom formula '
                    "and revert to the default weighted average."
                ),
            },
            "assistant_message": {
                "type": "string",
                "description": (
                    "The natural-language message shown to the user in the chat transcript "
                    "for THIS turn, explaining what you set/changed and why — same "
                    "requirement as update_draft's own `assistant_message` field."
                ),
            },
        },
        "required": ["formula", "assistant_message"],
    },
)

RESPOND_CONVERSATIONALLY_TOOL = ToolSpec(
    name="respond_conversationally",
    description=(
        "Reply to the user in free-text prose WITHOUT changing the draft in any way — no "
        "KPIs, weights, guidelines, scalar fields, or scoring_formula are touched when you "
        "call this. Use it when the user's latest message is exploratory/informational "
        "rather than a decision: an open question ('what are common KPI frameworks for "
        "vendor risk?'), a request to explain, compare, or discuss options, a follow-up "
        "question about something you already said, or a reaction that doesn't itself "
        "commit to anything. Also use it to report back what you found after an on-demand "
        "web_search the user explicitly asked for ('can you look that up'), when they "
        "haven't ALSO told you what to do with the result yet. Give a genuinely "
        "informative, specific answer — weave in real findings from web_search or the "
        "research context above when relevant, not vague generalities. "
        "Do NOT call this when the user has expressed a clear decision or preference "
        "('yes, use that one', 'I like the GDPR-based approach, add it', 'rename it to "
        "X', 'looks good, save it') — call update_draft (or update_scoring_formula) "
        "instead so the change actually happens, not just gets described. Do NOT call "
        "this when you are missing information you genuinely need before you can proceed "
        "— call ask_clarification instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "response": {
                "type": "string",
                "description": "Your free-text reply to show the user, as normal chat prose.",
            },
        },
        "required": ["response"],
    },
)

EDIT_KPIS_TOOL = ToolSpec(
    name="edit_kpis",
    description=(
        "PREFERRED way to change an EXISTING draft's KPIs (follow-up edits: change a weight, rename, "
        "remove, add, move, include/exclude from scoring, regenerate a rubric). Send a SMALL list of "
        "operations; the server applies them to the current draft, rebalances all leaf weights to sum "
        "exactly 100 with exact arithmetic, writes the guidelines of added/regenerated KPIs itself and "
        "keeps every other KPI (names, weights, rubrics) untouched. Output stays tiny however large the "
        "scorecard is. Use `update_draft` with a full `kpis` list ONLY for a wholesale restructuring, "
        "never for an edit of a few KPIs. Copy KPI names exactly as they appear in the current draft."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "minItems": 1,
                "maxItems": 40,
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "set_weight", "rename", "remove", "add", "move",
                                "set_included_in_scoring", "set_guidelines",
                            ],
                        },
                        "name": {"type": "string", "description": "Existing KPI name (for `add`: the NEW KPI's name)."},
                        "new_name": {"type": "string", "description": "rename only."},
                        "weight": {"type": "number", "minimum": 0, "maximum": 100,
                                   "description": "set_weight (required) / add (optional) — the KPI's final weight."},
                        "rebalance": {
                            "type": "string", "enum": ["proportional", "none"],
                            "description": "set_weight: scale ALL the OTHER leaves proportionally so the total stays "
                                           "100 (default) - so ONE op is enough for 'make X 8% and scale the "
                                           "others'; NEVER add an op per other KPI - or none.",
                        },
                        "parent_name": {"type": ["string", "null"],
                                        "description": "add / move: the (new) parent category, null = top level."},
                        "included_in_scoring": {"type": "boolean", "description": "set_included_in_scoring only."},
                        "rationale": {"type": "string",
                                      "description": "add / set_guidelines: one sentence on what the KPI measures "
                                                     "(used to write its rubric)."},
                    },
                    "required": ["op", "name"],
                },
            },
            "assistant_message": {
                "type": "string",
                "description": "REQUIRED. The user-facing explanation of what you changed (and the answer to any "
                               "question the user asked). Shown verbatim.",
            },
            "confirmed": {"type": "boolean", "description": "true only when the user told you to save the draft."},
        },
        "required": ["ops", "assistant_message"],
    },
)

_TOOLS = [
    ASK_CLARIFICATION_TOOL, UPDATE_DRAFT_TOOL, EDIT_KPIS_TOOL, UPDATE_SCORING_FORMULA_TOOL,
    RESPOND_CONVERSATIONALLY_TOOL,
]
_TOOLS_WITH_SEARCH = [*_TOOLS, WEB_SEARCH_TOOL]

# Bounds how many times propose_kpis will let the model call web_search within a single
# node visit (i.e. per LLM "turn" as MAX_LLM_TURNS_PER_HUMAN_TURN counts them) before
# forcing closure — offering only the non-search tools (which now include
# respond_conversationally — see RESPOND_CONVERSATIONALLY_TOOL) on the next call, which the
# model cannot route around since force_tool_use is always on. This mirrors the reference
# project's "cap ~3 search iterations then force closure" pattern, adapted to this
# project's strict-tool-choice mechanism instead of a prompt+regex loop. Kept as a genuine
# fallback/follow-up budget even now that `research_kpis` (below) does the primary,
# structured research fan-out once per session — e.g. for a later turn where the user
# pivots the domain, asks an ad hoc follow-up research question mid-conversation (see
# RESPOND_CONVERSATIONALLY_TOOL's own docstring/the module's "on-demand search" feature),
# or the fan-out itself was skipped/failed (see research_kpis).
MAX_WEB_SEARCH_CALLS_PER_PROPOSE = 3

# Session-level ceiling on propose_kpis's OWN ad hoc web_search usage — separate from, and
# in addition to, MAX_WEB_SEARCH_CALLS_PER_PROPOSE (which only bounds a single node VISIT,
# i.e. a single LLM sub-loop within one human turn). Without this, a long-lived session
# with many human turns could rack up unbounded real Gateway calls over its lifetime (e.g.
# 3 searches x 4 propose_kpis visits x dozens of human turns) even though each individual
# turn is itself bounded — exactly the "can't be abused into unbounded search cost" gap
# flagged for the new on-demand-mid-conversation search capability. Deliberately generous
# (not a tight per-message quota): a real user researching a scorecard across a long
# conversation should rarely hit it, and once hit, web_search simply stops being offered
# for the rest of the session — propose_kpis still works fine on its own knowledge/the
# research_kpis fan-out's findings, exactly like the graceful-degradation path when no
# web_search_client is wired in at all. Does NOT count research_kpis's own fan-out search
# calls (those are already separately, tightly bounded by MAX_RESEARCH_ROUNDS x
# MAX_CATEGORIES x MAX_SEARCH_CALLS_PER_RESEARCH_AGENT — see those constants) — mixing
# the two counters would conflate two independently-reasoned-about budgets for no benefit.
MAX_WEB_SEARCH_CALLS_PER_SESSION = 15


# --- Multi-agent research fan-out (research_kpis node) ----------------------------------
#
# Genuine concurrent fan-out, run ONCE per session (guarded by BuilderState.research_done),
# immediately before the first propose_kpis call: one Bedrock call decides 0-MAX_CATEGORIES
# named, business-recognizable KPI CATEGORIES for the user's stated domain/purpose — e.g.
# for a project-milestone quality scorecard: "Schedule", "Budget", "Quality" (a real example
# from the product owner: "Schedule (4 KPIs), Budget (4 KPIs), Quality (12 KPIs)") — never
# abstract "research angles" a business user wouldn't recognize. The category decision IS
# the research assignment: each category dispatches exactly one instance of the SAME bounded
# research-agent worker (`_run_research_agent` below — one definition, N concurrent
# invocations via `asyncio.gather`, mirroring judge.py's k-ensemble concurrency idiom), which
# researches and proposes KPIs scoped SPECIFICALLY to that one category — not a generic
# "angle" that might or might not end up mapping onto the final structure.
#
# **Category = Level-1 KpiDraft; its KPIs = Level-2 KpiDraft(parent_name=category)**: this
# reuses `draft_schema.py`'s existing, already-fully-supported-end-to-end (by
# `materialize_draft` and the real DB schema — see that module's docstring) hierarchy
# mechanism completely unchanged — a "category" is simply a `KpiDraft(level=1,
# parent_name=None)`, and each KPI researched under it is a `KpiDraft(level=2,
# parent_name=<category name>)`. No schema change of any kind; this is purely how THIS
# generation pipeline now shapes what it proposes. Categories carry NO weight of their own
# (see migration 0008_category_nodes_no_weight / draft_schema.py's "only leaf KPIs are
# weighted" rule) — purely organizational. Each category's relative IMPORTANCE is instead
# folded directly into its children's weights as a scaling factor (see
# `_merge_research_kpi_batches`), so every LEAF's weight already represents its final
# GLOBAL share of the whole scorecard, and the DB's weight-sum-to-100 rule is enforced
# across every leaf in the scorecard version at once, not per category.
#
# **KPI batching** (the mechanism, carried over unchanged in spirit from before this
# category restructure, that lets the TOTAL KPI count grow with research breadth instead of
# being capped by what one giant end-of-pipeline tool call could pack into a single JSON
# response): each research agent, once it has recorded its finding, makes ONE additional
# bounded tool call (`propose_kpi_batch`/`PROPOSE_KPI_BATCH_TOOL`) proposing its OWN small
# batch of fully-specified KPIs — name, weight, AND full 11-level guidelines each — grounded
# entirely in ITS OWN research. Every item in that batch is AUTOMATICALLY tagged
# `level=2, parent_name=<its category>` server-side (see `_validate_kpi_batch_items`) — a
# research agent never has to (and cannot) decide its own place in the hierarchy, since it
# IS that category's dedicated research assignment by construction. `research_kpis` merges
# every agent's batch (cross-category dedup + per-category weight-renormalize + folding
# each category's relative importance into its children's weights as a global scaling
# factor + a total safety cap— see `_merge_research_kpi_batches`) directly
# into `draft.kpis` BEFORE `propose_kpis` ever runs, so `propose_kpis`'s job shifts from
# "generate KPIs from scratch" to "review this already-comprehensive, already-categorized,
# already-grounded set and reconcile/confirm it with the user" (see `propose_kpis`'s own
# system prompt, which now also explicitly preserves the category structure on any further
# reconciliation edit).
#
# All findings (KPI batches included) are also consolidated into
# `BuilderState.research_findings` and woven into every subsequent propose_kpis system
# prompt for the rest of the session (see `_format_research_findings_for_prompt`), so any
# later ad hoc adjustment is still traceably grounded in the same real, cited research.
#
# Bounds/why this doesn't blow through MAX_LLM_TURNS_PER_HUMAN_TURN: `research_kpis` is a
# separate node from propose_kpis and never touches `llm_turn_count` (that counter only
# tracks consecutive propose_kpis node VISITS within one human turn — see
# MAX_LLM_TURNS_PER_HUMAN_TURN's own docstring below). Because research_done guards it to
# run at most once per session (not once per propose_kpis visit), it adds one bounded burst
# of latency on the session's first turn and is a no-op on every later human turn — it
# cannot itself cause propose_kpis to loop more times, so MAX_LLM_TURNS_PER_HUMAN_TURN's
# existing value needs no adjustment. The one extra `propose_kpi_batch` call each research
# agent makes similarly doesn't touch MAX_SEARCH_CALLS_PER_RESEARCH_AGENT (it isn't a
# web_search call) or any per-propose_kpis budget.
# Safety ceilings only (runaway protection) — NOT targets. The real category/KPI counts come
# from the domain's breadth and from a requested KPI count (see `_plan_fanout`). These used to
# be 5 categories x 6 KPIs/batch x a 30 total cap, which together capped every scorecard at
# ~15-20 KPIs however many were asked for.
MAX_CATEGORIES = 24
MAX_SEARCH_CALLS_PER_RESEARCH_AGENT = 2

# How many KPIs one research agent proposes in its own batch (per category), and the hard
# ceiling on the TOTAL merged/deduped KPI count `research_kpis` will hand to propose_kpis —
# a safety cap ("can't run away"), deliberately NOT a fixed target (real coverage is driven
# by what research actually surfaced). Both bumped slightly from this feature's earlier,
# pre-category values (4 / 24): each batch now maps 1:1 onto a whole thematic CATEGORY (e.g.
# "Quality") rather than one narrow "angle", and a category — per the product owner's own
# real example ("Quality (12 KPIs)" vs. "Schedule (4 KPIs)") — can legitimately need more
# than 4 KPIs of its own even before a second research round (see MAX_RESEARCH_ROUNDS below)
# deepens it further.
#
# KPIs are now proposed in BOUNDED chunks: each `propose_kpi_batch` call asks for at most
# `KPIS_PER_BATCH_CALL` fully-specified KPIs (11-level rubrics make big calls large, and a
# very large tool call is what hits the output-token limit), and an agent keeps asking for
# further chunks (see `_propose_category_kpis`) while it reports more distinct KPIs remain
# and its quota is unmet — bounded by `MAX_BATCH_CALLS_PER_AGENT` / `MAX_KPIS_PER_CATEGORY`.
KPIS_PER_BATCH_CALL = 12  # names/weights/rationales only (guidelines are written separately)
MAX_KPIS_PER_CALL = 24  # schema ceiling for one call's `kpis` array
MAX_BATCH_CALLS_PER_AGENT = 10
MAX_KPIS_PER_CATEGORY = 60
MAX_TOTAL_MERGED_KPIS = 300

# --- Requested KPI count ("give me 35 KPIs") --------------------------------------------
# A requested count N must land within +/- KPI_TARGET_TOLERANCE of N (fewer ONLY with an
# explicit explanation). Sizing: categories ~ N / KPIS_PER_CATEGORY_TARGET; each agent is asked
# for TARGET_OVERSHOOT x its share (dedup loses some); a reconciliation step then deepens up to
# MAX_DEEPEN_ROUNDS times if short, and prunes the lowest-weighted leaves if over.
KPI_TARGET_TOLERANCE = 2
KPIS_PER_CATEGORY_TARGET = 6
TARGET_OVERSHOOT = 1.2
MAX_DEEPEN_ROUNDS = 3
# A requested count above this runs the category fan-out even with no web search configured:
# one `update_draft` tool call cannot reliably carry that many rubric-bearing KPIs.
FANOUT_WITHOUT_SEARCH_MIN_TARGET = 12

# --- Iterative / multi-round research (bounded) -----------------------------------------
#
# `research_kpis` runs round 1 (decide categories -> fan out one agent per category -> merge
# into the category/KPI hierarchy), then — if the master isn't confident that round's
# research genuinely covers the domain, and `MAX_RESEARCH_ROUNDS` hasn't been reached yet —
# launches ONE more bounded round (see `_assess_research_coverage`). A category composes
# cleanly across rounds with ZERO extra plumbing: `_assess_research_coverage` can propose
# either a genuinely NEW category (a name never used before) or the EXACT same name as an
# existing category to "deepen" it (more KPIs for a category whose round-1 coverage felt
# shallow) — either way, round 2's research agent(s) tag their batch with that category name
# exactly like round 1 did, and the merge step (which re-groups ALL rounds' findings by
# category name every time it runs — see `_merge_research_kpi_batches`) naturally folds a
# "deepen" round's new KPIs in alongside that same category's round-1 KPIs (deduped,
# renormalized together), while a genuinely new category name naturally becomes its own
# additional Level-1 node. No separate "is this a new or existing category" branch is needed
# anywhere in this module — grouping by name is the entire mechanism.
#
# Why the cap is 2, not 3+ (explicit latency/cost reasoning, not a reflexive round number):
# each round is itself 2-5 CONCURRENT research agents, and each agent makes up to
# MAX_SEARCH_CALLS_PER_RESEARCH_AGENT (2) web_search calls PLUS one record_research_finding
# call PLUS one propose_kpi_batch call — i.e. up to ~4 sequential Bedrock/web_search round-
# trips per agent, observed live (per this project's own prior live-testing passes) to take
# 20-90+ seconds for a SINGLE round depending on model/Gateway latency. A second round adds
# one more `_assess_research_coverage` call (cheap, one Bedrock call) plus a second full
# agent fan-out (another 20-90+s) — already a meaningful chunk of a synchronous HTTP
# request's total latency. A THIRD round would compound that same cost again for diminishing
# returns while risking a chat turn that feels broken/hung to a user waiting on a single HTTP
# response. 2 is therefore the default; bumping it is a one-line change if the product later
# decides the extra latency is worth it for specific domains.
#
# Interaction with MAX_LLM_TURNS_PER_HUMAN_TURN: unchanged from before this category
# restructure — every round of the loop, the category-decision call, AND every
# `_assess_research_coverage` call all happen INSIDE this one `research_kpis` node
# invocation (a plain Python `while` loop below, not additional LangGraph nodes/edges), which
# still runs at MOST once per session and still never touches `llm_turn_count`.
MAX_RESEARCH_ROUNDS = 2

# --- Quality gate (self-critique layer via Jev/OpenRouter — see app/ai/jev_client.py) ---
#
# Three checkpoints, each wrapping an existing decision point with a bounded
# instruction/answer "does this genuinely serve the request?" rating from Jev — reframed
# around categories, not removed or weakened:
#   1. The decided CATEGORY PLAN (research_kpis) — see _decide_categories_with_gate. Rates
#      the plan (category names + their research focus) against the user's actual request.
#   2. Each category research agent's synthesized finding (_run_research_agent), right after
#      record_research_finding — rated against THAT agent's assigned category/focus
#      specifically, never a generic "did you do something useful" check.
#   3. propose_kpis's final per-visit decision (ask_clarification/update_draft/
#      update_scoring_formula/respond_conversationally) — unchanged by this restructure.
#
# MAX_QUALITY_GATE_RETRIES=2 (the same "original attempt + up to 2 revisions" shape used
# throughout this module already — see MAX_RESEARCH_ROUNDS/MAX_WEB_SEARCH_CALLS_PER_
# PROPOSE for the same "small, explicit, bounded" pattern): a checkpoint that never clears
# QUALITY_GATE_THRESHOLD (0.75, from jev_client.py) after 2 revision attempts proceeds
# with its BEST-SCORING attempt seen so far rather than hanging the turn or silently
# dropping the result. `quality_gate()` itself never raises (see jev_client.py), so an
# unreachable Jev degrades every checkpoint to "passed, no retry" transparently — these
# retry loops only ever engage on a REAL, successfully-obtained low score.
MAX_QUALITY_GATE_RETRIES = 1  # was 2: live runs spent ~15 of 17 minutes in gate retries

# --- Quality-gate ADVISOR/critique step (actor-critic / Reflexion-style self-refinement) --
#
# Replaces the old "the score was low, improve it" generic revision note each of the 3
# checkpoints used to feed into its retry with a concrete, specific critique produced by a
# real LLM call — the pattern (an actor produces an attempt; a critic — here, re-using the
# SAME actor model rather than a separate verifier model — inspects the attempt plus the
# scorer's verdict and produces pointed feedback; the actor's NEXT attempt is conditioned on
# that feedback) mirrors "Reflexion"-style self-refinement loops with an external judge
# (Jev plays the role of the scorer/judge here, exactly as it already did; this step is the
# missing "verbal reinforcement"/critique layer between a low score and the next attempt).
#
# Deliberately NOT a separate LangGraph node: all three checkpoints below are themselves
# plain bounded Python retry loops living INSIDE existing node functions (research_kpis's
# category-planning call, each concurrent research agent spawned via asyncio.gather outside
# the graph entirely, and propose_kpis's own decision loop) rather than graph nodes/edges of
# their own — see this module's own docstring ("propose_kpis is the one node that calls the
# LLM..."). `_generate_quality_gate_critique` below is this same kind of unit: a shared,
# bounded helper called from inside each of those existing retry loops, never introducing a
# new loop or raising past itself (mirrors every other "never raise past this layer"
# contract in this module) — so MAX_QUALITY_GATE_RETRIES stays the one hard cap on how many
# times any checkpoint ever re-attempts anything.
#
# Uses the SAME Bedrock client/model (GLM-5, zai.glm-5) and the SAME force_tool_use=True
# structured-tool-call pattern as every other call in this module — Jev/OpenRouter is a
# pure scorer and is NEVER used to generate text (see jev_client.py's own module docstring).

CRITIQUE_QUALITY_GATE_TOOL = ToolSpec(
    name="critique_response",
    description=(
        "Give concrete, specific feedback on why the response below scored poorly on an "
        "automated quality check, and exactly what to change to fix it next attempt. Cite "
        "actual content from the response being critiqued — never a generic 'be better' or "
        "'try harder' note that could apply to any response."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "specific_problems": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": (
                    "1-4 SPECIFIC problems with the response above, each naming something "
                    "concrete in it (a missing element the task needed, an unsupported or "
                    "wrong claim, something that doesn't match what was actually asked, an "
                    "irrelevant tangent, etc.) — never a vague restatement of the score."
                ),
            },
            "concrete_fix": {
                "type": "string",
                "description": (
                    "A specific, actionable instruction for exactly what to do differently "
                    "on the next attempt — concrete enough that a genuinely different "
                    "attempt would visibly follow it, not generic advice to 'improve'."
                ),
            },
        },
        "required": ["specific_problems", "concrete_fix"],
    },
)

_CRITIQUE_RETRY_CONTEXT_TEMPLATE = """

THIS IS THE SECOND FAILURE for this task — a first critique was already given and acted on:

Critique given after the FIRST failure:
{previous_critique}

What the response looked like BEFORE that first critique was applied:
{previous_output}

Despite that critique, the revised response above STILL scored below threshold. Compare \
what actually changed (or didn't) between the before-version above and the response being \
critiqued now, work out specifically why the earlier fix fell short or missed the point, \
and give a REFINED critique that addresses that — do not just repeat the same advice \
verbatim."""

_CRITIQUE_SYSTEM_PROMPT_TEMPLATE = """You are the quality-gate ADVISOR for the Quality \
Scorecard System's scorecard-design assistant. An automated quality check (Jev) just \
scored one of the assistant's own responses {gate_score:.2f}/1.0 — below the required \
{threshold} threshold — meaning it did not genuinely/fully satisfy the task below. Your job \
is NOT to redo the task yourself; it is to give the assistant a concrete, specific critique \
of exactly what is wrong with its response and exactly what to fix, so its next attempt is \
a genuine, targeted improvement rather than a blind re-roll.

TASK / instruction the response below was supposed to satisfy:
{task_context}

THE RESPONSE THAT WAS PRODUCED (scored {gate_score:.2f}/1.0, below {threshold}):
{produced_output}
{retry_context}

Call `critique_response` exactly once with specific_problems (concrete, citing real content \
above — never vague) and a concrete_fix (specific enough that a genuinely different next \
attempt would follow from it)."""


async def _generate_quality_gate_critique(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    *,
    task_context: str,
    produced_output: str,
    gate_score: float,
    threshold: float = QUALITY_GATE_THRESHOLD,
    previous_critique: str | None = None,
    previous_output: str | None = None,
) -> str:
    """Actor-critic/Reflexion-style self-refinement step — see the module comment above.
    Turns a below-threshold Jev score into a concrete, specific critique the NEXT retry
    attempt can act on, in place of the old generic "the score was low, improve it" note.
    Runs on the SAME GLM-5 Bedrock client/model as the rest of the chat pipeline (never
    Jev/OpenRouter — see jev_client.py's own module docstring: Jev stays a pure scorer, never
    a generator), via the identical `force_tool_use=True` structured-tool-call pattern used
    everywhere else in this module.

    Called only from inside an ALREADY-bounded `MAX_QUALITY_GATE_RETRIES` retry loop (the
    three checkpoints below) — introduces no loop of its own, and never raises past itself: a
    failed/malformed critique call just falls back to the old generic revision note so an
    advisor-call failure can never block a turn (mirrors every other "never raise past this
    layer" contract in this module, e.g. `_run_research_agent`'s own whole-agent try/except).

    `previous_critique`/`previous_output` are set only on the SECOND failure of the same
    task (i.e. the retry attempt that had already applied the FIRST critique also failed its
    gate) — `previous_output` is what the response looked like BEFORE that first critique,
    so the advisor can see what the model actually changed in response to it and refine the
    critique instead of repeating the same advice (exactly what the task asked for: "point
    out specifically why the fix still fell short and refine the critique further")."""
    retry_context = ""
    if previous_critique and previous_output is not None:
        retry_context = _CRITIQUE_RETRY_CONTEXT_TEMPLATE.format(
            previous_critique=previous_critique, previous_output=previous_output
        )
    system_prompt = _CRITIQUE_SYSTEM_PROMPT_TEMPLATE.format(
        gate_score=gate_score,
        threshold=threshold,
        task_context=task_context or "(nothing yet)",
        produced_output=produced_output or "(empty)",
        retry_context=retry_context,
    )
    fallback = (
        f"An automated quality check scored your previous response {gate_score:.2f}/1.0 "
        f"(threshold {threshold}). Reconsider it carefully and provide a genuinely improved "
        "response that more fully and correctly addresses the task above."
    )
    try:
        result = await asyncio.to_thread(
            bedrock.converse,
            messages=[{"role": "user", "content": [{"text": "Critique the response above."}]}],
            system=system_prompt,
            tools=[CRITIQUE_QUALITY_GATE_TOOL],
            force_tool_use=True,
            model_id=model_id,
        )
    except Exception:  # noqa: BLE001 — an advisor-call failure must never block a retry
        logger.warning(
            "quality-gate critique call failed; falling back to a generic revision note.", exc_info=True
        )
        return fallback

    if not (result.is_tool_use and result.tool_name == "critique_response"):
        logger.warning(
            "quality-gate critique call returned no usable critique_response tool call "
            "(stop_reason=%r); falling back to a generic revision note.",
            getattr(result, "stop_reason", None),
        )
        return fallback

    data = result.tool_input or {}
    problems = [str(p).strip() for p in (data.get("specific_problems") or []) if str(p).strip()]
    fix = str(data.get("concrete_fix") or "").strip()
    if not (problems or fix):
        return fallback

    lines = [
        f"An automated quality check scored your previous response {gate_score:.2f}/1.0 "
        f"(threshold {threshold}). Here is specifically what fell short and what to fix:"
    ]
    lines.extend(f"- {p}" for p in problems)
    if fix:
        lines.append(f"What to do differently now: {fix}")
    return "\n".join(lines)


DECIDE_CATEGORIES_TOOL = ToolSpec(
    name="decide_categories",
    description=(
        "Decide which distinct, business-recognizable KPI CATEGORIES this scorecard's "
        "domain calls for — e.g. for a project-milestone quality scorecard: 'Schedule', "
        "'Budget', 'Quality'. Each category becomes its own top-level grouping in the "
        "final scorecard AND is handed to one independent research agent, running "
        "concurrently with the others, that researches and proposes KPIs belonging "
        "specifically to that category. This is NOT a list of abstract research topics — "
        "it IS the category structure the user will see grouping their KPIs, so pick names "
        "a business user in this domain would immediately recognize, never a vague research "
        "theme."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_CATEGORIES,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "The category's name EXACTLY as it should appear to the "
                                "user, e.g. 'Schedule', 'Budget', 'Quality' — short, plain, "
                                "business-recognizable."
                            ),
                        },
                        "focus": {
                            "type": "string",
                            "description": (
                                "A specific, researchable brief for this category's "
                                "dedicated research agent: what to investigate on the web "
                                "to ground this category's KPIs and quantitative "
                                "thresholds (not a vague topic)."
                            ),
                        },
                        "initial_weight": {
                            "type": ["number", "null"],
                            "description": (
                                "OPTIONAL relative IMPORTANCE (0-100) of this category among "
                                "the OTHERS you're deciding now (e.g. Quality might "
                                "reasonably outweigh Schedule for a milestone scorecard) — "
                                "NOT a weight stored on the category itself (categories have "
                                "no weight of their own); it is only a scaling factor folded "
                                "into this category's own KPIs' weights once research is in, "
                                "so this only needs to be your best relative judgment now — "
                                "omit/null for an equal default share instead."
                            ),
                        },
                    },
                    "required": ["name", "focus"],
                },
            },
        },
        "required": ["categories"],
    },
)

RECORD_RESEARCH_FINDING_TOOL = ToolSpec(
    name="record_research_finding",
    description=(
        "Record your research finding for YOUR SINGLE assigned category. Call this exactly "
        "once, after using your web_search budget (if you used it)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A concise summary of what you found for this category.",
            },
            "suggested_kpis": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["name", "rationale"],
                },
                "description": "KPI name/rationale candidates this category's research supports. Empty array if none.",
            },
            "suggested_thresholds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "value_or_range": {"type": "string"},
                        "source_note": {"type": "string"},
                    },
                    "required": ["metric", "value_or_range"],
                },
                "description": (
                    "Concrete quantitative benchmark(s) grounded in what you found. Empty array if none."
                ),
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["title", "url"],
                },
                "description": "Real title+url of anything you cited. Empty array if you found nothing usable.",
            },
        },
        "required": ["summary"],
    },
)

PROPOSE_KPI_BATCH_TOOL = ToolSpec(
    name="propose_kpi_batch",
    description=(
        "Propose YOUR OWN batch of KPIs for YOUR CATEGORY, grounded entirely in the "
        "research you just recorded (at most the number you are told per call; set `has_more` "
        "when more distinct ones remain). Give ONLY each KPI's name, relative weight and a "
        "one-sentence rationale — do NOT write score-level guidelines: the 0-10 rubrics are "
        "written for every KPI in a separate, parallel step. Call this exactly once, "
        "immediately after record_research_finding. Weight each KPI relative to the OTHER "
        "KPIs IN THIS CATEGORY ONLY (every category's children get renormalized to sum to 100 "
        "WITHIN that category afterward). Return an EMPTY array only if your research "
        "genuinely didn't surface anything KPI-worthy for this category."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kpis": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_KPIS_PER_CALL,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "A specific, distinct KPI name."},
                        "weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Relative weight among just this category's KPIs (batch sums to ~100).",
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "ONE sentence (<= 25 words): what is measured and why it matters here, citing "
                                "your research's thresholds/benchmarks where relevant. Used to write the rubric."
                            ),
                        },
                    },
                    "required": ["name", "weight", "rationale"],
                },
            },
            "has_more": {
                "type": "boolean",
                "description": (
                    "true if there are MORE distinct, genuinely valuable KPIs for this category that you "
                    "did not include (you will be asked again for the next chunk); false if this batch "
                    "exhausts what the category warrants. Never pad: false is the right answer when the "
                    "remaining ideas would be weak or overlap what you already proposed."
                ),
            },
        },
        "required": ["kpis"],
    },
)

ASSESS_RESEARCH_COVERAGE_TOOL = ToolSpec(
    name="assess_research_coverage",
    description=(
        "Given the categories and KPIs gathered so far for this domain, decide whether "
        "coverage is genuinely SUFFICIENT to proceed to KPI proposal now, or whether an "
        "important, DISTINCT category/gap remains unresearched. This is the confidence "
        "check between research rounds — be honest: most domains ARE adequately covered "
        "after one round; only report insufficient coverage when you can name a real, "
        "specific, meaningfully different category or gap the research so far hasn't "
        "touched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "sufficient": {
                "type": "boolean",
                "description": (
                    "true if coverage is genuinely adequate to propose a comprehensive, "
                    "well-grounded, well-categorized KPI set now; false only if there's a "
                    "real, specific gap."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "Brief reasoning for this judgment, grounded in the actual categories/"
                    "findings/KPIs listed above — not a generic statement."
                ),
            },
            "next_categories": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_CATEGORIES,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "A category name. Use the EXACT SAME name as an existing "
                                "category above to deepen/extend it with more research "
                                "(its new KPIs join its existing ones), or a genuinely NEW "
                                "name to add a brand-new category."
                            ),
                        },
                        "focus": {
                            "type": "string",
                            "description": "A specific, researchable focus for this round's research agent.",
                        },
                        "initial_weight": {
                            "type": ["number", "null"],
                            "description": "Same meaning as decide_categories' initial_weight. Optional.",
                        },
                    },
                    "required": ["name", "focus"],
                },
                "description": (
                    "ONLY populate when sufficient=false: 1 or more categories (new, or an "
                    "existing name to deepen) that specifically target the gap identified "
                    "in `reasoning`. Leave empty when sufficient=true."
                ),
            },
        },
        "required": ["sufficient", "reasoning", "next_categories"],
    },
)

_DECIDE_CATEGORIES_SYSTEM_PROMPT = f"""You are the research-planning step for the Quality \
Scorecard System's scorecard-design assistant. Given what the user has said so far about \
the scorecard they want, decide which DISTINCT, business-recognizable KPI CATEGORIES (from 0 up to as \
many as the domain genuinely has, never more than {MAX_CATEGORIES}) this domain calls for — the \
top-level groupings the user will actually SEE \
organizing their KPIs, each handed to its own dedicated research agent.

Guidelines:
- Categories must be real, business-recognizable groupings a stakeholder in this domain \
would immediately understand — e.g. for a project-milestone quality scorecard: "Schedule", \
"Budget", "Quality" (a real example: Schedule had 4 KPIs, Budget had 4, Quality had 12). \
NEVER invent abstract "research angle" labels nobody would recognize as a category of their \
scorecard.
- Each category must be meaningfully DISTINCT — never two categories that are really the \
same concern worded two ways.
- Choose the NUMBER of categories based on the domain's actual breadth, not a fixed default: \
0 if the domain is narrow/simple enough that no dedicated category structure or research \
fan-out is needed (an ordinary web_search tool is still available later for ad hoc \
lookups); 1 if there is exactly one clear grouping; 2-8 for a domain that genuinely spans \
multiple distinct concerns (most real scorecards land here); more for a very broad domain or \
when the sizing note below asks for a large KPI count.
- Each category needs a short `name` (exactly as it should appear to the user) and a \
specific, researchable `focus` for its dedicated research agent. `initial_weight` is \
optional — your best relative guess at this category's share of the total score; omit it \
for an equal default share.

Base your decision on what the user actually said below — the structured draft fields may \
still be empty/null this early in the conversation (e.g. on the very first message, before \
any clarifying question has been answered), so the user's own words are very likely your \
ONLY signal for the domain right now. Do not treat an empty draft as "not enough \
information" if the user's message already describes a clear domain/purpose.

What the user has said so far (most recent message last):
{{conversation_context}}

Current draft (JSON, may still be mostly empty this early — see above): {{draft_json}}
{{sizing_hint}}
Call `decide_categories` exactly once."""

_RESEARCH_WORKER_SYSTEM_PROMPT_TEMPLATE = """You are ONE independent research agent, one \
of several running concurrently, each responsible for ONE category of a quality \
scorecard's KPIs and quantitative thresholds.

Your assigned category: "{category}"
Your specific research focus: "{focus}"

Use `web_search` (you have a budget of at most {max_calls} search call(s) this session) to \
find real, current, checkable information — published industry benchmarks, standards, \
frameworks, or regulatory requirements relevant to YOUR category only. Then call \
`record_research_finding` exactly once. Stay focused on your category — do not try to cover \
the whole scorecard; other agents are covering the other categories."""

_PROPOSE_KPI_BATCH_SYSTEM_PROMPT_TEMPLATE = """You are the SAME research agent that just \
investigated category "{category}" (focus: "{focus}") and recorded this finding:

Summary: {summary}
Suggested KPI ideas: {suggested_kpis}
Suggested quantitative thresholds: {suggested_thresholds}
Sources: {sources}

Now propose a batch of KPIs for YOUR category, grounded in what you JUST found above. \
Each needs: a specific, distinct name; a weight (0-100, relative to the other KPIs in THIS \
CATEGORY only — other agents propose their own categories' batches independently, and every \
category's children get renormalized together afterward); and a one-sentence rationale (what \
is measured, plus the benchmark/threshold from your research where there is one). Do NOT write \
the 0-10 score guidelines — a separate step writes the full rubric for each KPI from your name \
and rationale, so keep this call short. Call `propose_kpi_batch` exactly once.{quota_note}"""

_ASSESS_COVERAGE_SYSTEM_PROMPT = f"""You are the research-planning step for the Quality \
Scorecard System's scorecard-design assistant, reviewing the results of research round \
{{rounds_so_far}} to decide whether to propose KPIs now or investigate a genuine gap first. \
THIS is the real confidence check the multi-round research loop is built on — your \
judgment here, grounded in the actual findings below, decides whether a second (bounded) \
round of concurrent research agents runs at all.

What the user has said so far (most recent message last):
{{conversation_context}}

Categories already investigated so far: {{categories_covered}}

Consolidated findings and KPIs gathered so far (grouped by category):
{{findings_summary}}

Decide honestly: is this category structure and KPI coverage genuinely SUFFICIENT to \
propose a comprehensive, well-grounded scorecard for this domain now, or is there a real, \
DISTINCT gap — an important category this domain needs that none of the above covers, or an \
existing category whose coverage feels genuinely shallow? Most domains ARE adequately \
covered after one round of focused research — only report `sufficient: false` when you can \
name a SPECIFIC, meaningful, missing category or gap, never merely "more research is always \
better" or a marginal refinement already covered above.

{{sizing_hint}}
If (and only if) insufficient, propose 1 to {MAX_CATEGORIES} categories to research next: \
use the EXACT SAME name as an existing category above to deepen it (its new KPIs join its \
existing ones), or a genuinely NEW name for a category not yet covered — whichever \
specifically targets the gap you identified. The whole point of a second round is covering \
NEW ground (a new category) or going deeper where it's genuinely thin (an existing one), \
never re-searching territory that's already adequately covered.

Call `assess_research_coverage` exactly once."""


@dataclass
class ResearchFinding:
    """Structured output of ONE research-agent invocation (see `_run_research_agent`) —
    what `research_kpis` fans out N of concurrently (one per CATEGORY — see the module
    comment above `MAX_CATEGORIES`) and consolidates into `BuilderState.research_findings`.
    `degraded=True` marks a finding produced by the graceful-failure path (the agent's own
    Bedrock/web_search calls raised, or the model never called `record_research_finding`)
    rather than a real recorded finding — see `research_kpis`'s consolidation step, which
    drops a degraded finding with no usable content instead of feeding empty noise into
    propose_kpis's context.

    `proposed_kpis`: this agent's own small batch of fully-specified KPI dicts (see
    `PROPOSE_KPI_BATCH_TOOL` — each already has name/weight/guidelines PLUS
    `level=2`/`parent_name=self.category` forced on by `_validate_kpi_batch_items`), the
    actual mechanism behind "KPIs arrive in batches, already tagged with their category, not
    one giant end-of-pipeline proposal". Always `[]` for a degraded finding."""

    category: str
    summary: str = ""
    suggested_kpis: list[dict[str, str]] = field(default_factory=list)
    suggested_thresholds: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    proposed_kpis: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False

    def has_content(self) -> bool:
        return bool(
            self.summary or self.suggested_kpis or self.suggested_thresholds or self.sources or self.proposed_kpis
        )


@dataclass
class CoverageAssessment:
    """Result of one `assess_research_coverage` call (see `_assess_research_coverage`) —
    THE confidence-check mechanism `research_kpis` uses to decide whether to launch
    another bounded research round. `sufficient=True` (including the fail-safe default
    used on any malformed/unusable tool response or failed call — see
    `_assess_research_coverage`'s own docstring) always means "stop here and proceed to
    propose_kpis": a broken or ambiguous assessment can only ever cause the loop to STOP
    early, never spin an extra round on garbage input, and never loop forever."""

    sufficient: bool
    reasoning: str
    next_categories: list[dict[str, Any]]


def _gate_time_left(turn_started_at: datetime | None) -> bool:
    """False once the turn has run longer than `settings.quality_gate_budget_seconds`: from then
    on no gate rating, critique or revision STARTS (their results can no longer be afforded).
    Unknown start (tests, direct calls) = always allowed."""
    if turn_started_at is None:
        return True
    now = datetime.now(turn_started_at.tzinfo) if turn_started_at.tzinfo else datetime.utcnow()  # noqa: DTZ003
    return (now - turn_started_at).total_seconds() < get_settings().quality_gate_budget_seconds


async def _gate(
    jev_client: JevClientProtocol | None,
    *,
    instruction: str,
    answer: str,
    turn_started_at: datetime | None,
) -> QualityGateResult:
    """`quality_gate` + the per-turn time budget (see `_gate_time_left`): over budget the gate is
    skipped and reported as passed/degraded, exactly like an unreachable Jev."""
    if not _gate_time_left(turn_started_at):
        logger.info("quality gate skipped: per-turn gate time budget exhausted.")
        return QualityGateResult(passed=True, score=None, degraded=True)
    return await quality_gate(jev_client, instruction=instruction, answer=answer)


def _decide_categories(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    draft: ScorecardDraft,
    conversation_context: str,
    revision_feedback: str | None = None,
    sizing_hint: str | None = None,
) -> list[dict[str, Any]]:
    """One synchronous Bedrock call (run via `asyncio.to_thread` by the caller) deciding
    how many/which KPI CATEGORIES this domain warrants — the top-level groupings the user
    will see, each also doubling as one research agent's assignment (see the module comment
    above `MAX_CATEGORIES`). Never raises past this function's own belt-and-suspenders
    try/except at the call site in `research_kpis` — a malformed or missing tool response
    here is treated as "no categories decided", not a crash.

    `conversation_context` (the user's own messages so far — see `research_kpis`) is the
    critical signal on a session's very first turn, when `draft` is still entirely empty:
    research_kpis runs BEFORE propose_kpis has ever had a chance to populate the structured
    draft fields, so the draft alone would tell this call nothing about the domain yet.

    `revision_feedback` (set only on a retry — see `_decide_categories_with_gate`,
    quality-gate checkpoint 1) appends Jev's below-threshold verdict to the system prompt
    so a revised plan is a genuine reconsideration, not a blind re-roll of the same call.

    Returns a list of `{"name": str, "focus": str, "initial_weight": float | None}`."""
    system_prompt = _DECIDE_CATEGORIES_SYSTEM_PROMPT.format(
        draft_json=json.dumps(draft.model_dump(mode="json")),
        conversation_context=conversation_context or "(nothing yet)",
        sizing_hint=f"\n{sizing_hint}\n" if sizing_hint else "",
    )
    if revision_feedback:
        system_prompt += f"\n\n{revision_feedback}"
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": "Decide the KPI categories for this scorecard."}]}],
        system=system_prompt,
        tools=[DECIDE_CATEGORIES_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_name != "decide_categories":
        return []
    raw_categories = (result.tool_input or {}).get("categories") or []
    return _clean_category_items(raw_categories)


def _categories_to_gate_text(categories: list[dict[str, Any]]) -> str:
    """Renders a decided category plan as plain text for the quality gate's `answer` (see
    `_decide_categories_with_gate`) — an empty plan is itself a valid, ratable answer ("the
    model judged no dedicated category structure/research was needed"), not a special
    case."""
    if not categories:
        return (
            "(No dedicated KPI categories were planned — the domain was judged "
            "narrow/simple enough that no category structure or research fan-out is needed.)"
        )
    return "Planned KPI categories:\n" + "\n".join(
        f'- "{c["name"]}" — focus: {c["focus"]}'
        + (f" (initial_weight={c['initial_weight']})" if c.get("initial_weight") is not None else "")
        for c in categories
    )


async def _decide_categories_with_gate(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    draft: ScorecardDraft,
    conversation_context: str,
    jev_client: JevClientProtocol | None,
    session_id: str,
    turn_started_at: datetime | None,
    sizing_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Quality-gate checkpoint 1 (see the module comment above `MAX_QUALITY_GATE_RETRIES`):
    wraps `_decide_categories` with a Jev rating of how well the decided CATEGORY PLAN
    serves the user's actual request (instruction=`conversation_context`, answer=the plan
    itself). Below `QUALITY_GATE_THRESHOLD`, the master revises its plan — bounded at
    `MAX_QUALITY_GATE_RETRIES` revisions — then proceeds with the best-scoring attempt
    seen (never hangs, never silently drops a low-scoring plan)."""
    instruction = conversation_context or "(nothing yet)"
    best_categories: list[dict[str, Any]] = []
    best_score = -1.0
    revision_feedback: str | None = None
    latest_critique: str | None = None
    # Tracks the PREVIOUS attempt's rendered plan text, so a second-failure critique can see
    # what actually changed in response to the first critique (see
    # `_generate_quality_gate_critique`'s own docstring).
    previous_categories_text: str | None = None

    for attempt in range(MAX_QUALITY_GATE_RETRIES + 1):
        categories = await asyncio.to_thread(
            _decide_categories, bedrock, model_id, draft, conversation_context, revision_feedback, sizing_hint
        )
        categories_text = _categories_to_gate_text(categories)
        gate = await _gate(jev_client, instruction=instruction, answer=categories_text, turn_started_at=turn_started_at)

        if gate.degraded:
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate",
                "Category plan quality check: Jev was unreachable — gate treated as passed "
                "(graceful degradation).",
            )
            return categories  # Jev unreachable — gate passed by policy; no point retrying.
        assert gate.score is not None  # guaranteed whenever degraded=False
        if gate.score > best_score:
            best_categories, best_score = categories, gate.score

        if gate.passed:
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate",
                f"Category plan quality check scored {gate.score:.2f} (>= {QUALITY_GATE_THRESHOLD}) — "
                + ("passed after revision." if attempt > 0 else "passed."),
            )
            return categories

        if attempt < MAX_QUALITY_GATE_RETRIES:
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate_retry",
                f"Quality check scored {gate.score:.2f} (below {QUALITY_GATE_THRESHOLD}) — "
                "revising the category plan…",
            )
            # Advisor/critique step (see the module comment above MAX_QUALITY_GATE_RETRIES):
            # a concrete, specific critique of THIS plan in place of a generic "try harder"
            # note — on the second failure, also carries what the first critique suggested
            # and what actually changed, so the critique is refined rather than repeated.
            latest_critique = await _generate_quality_gate_critique(
                bedrock, model_id,
                task_context=instruction,
                produced_output=categories_text,
                gate_score=gate.score,
                previous_critique=latest_critique,
                previous_output=previous_categories_text,
            )
            previous_categories_text = categories_text
            # The retried plan also sees the plan being critiqued, not just the critique.
            revision_feedback = (
                f"The category plan you produced last attempt was:\n{categories_text}\n\n{latest_critique}"
            )

    await emit_turn_event(
        session_id, turn_started_at, "master", "quality_gate",
        f"Category plan quality check still below {QUALITY_GATE_THRESHOLD} after "
        f"{MAX_QUALITY_GATE_RETRIES} revision(s) (best score {best_score:.2f}) — "
        "proceeding with the best attempt.",
    )
    return best_categories


def _clean_category_items(raw_items: list[Any]) -> list[dict[str, Any]]:
    """Shared cleaning logic for a list of `{name, focus, initial_weight}` dicts, however
    they were produced (`decide_categories`'s `categories` or `assess_research_coverage`'s
    `next_categories` — both use the identical shape) — factored out of `_decide_categories`
    so `_assess_research_coverage` doesn't duplicate the same defensive parsing."""
    cleaned: list[dict[str, Any]] = []
    for raw in raw_items[:MAX_CATEGORIES]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        focus = str(raw.get("focus") or "").strip()
        if not (name and focus):
            continue
        raw_weight = raw.get("initial_weight")
        initial_weight: float | None
        try:
            initial_weight = float(raw_weight) if raw_weight is not None else None
        except (TypeError, ValueError):
            initial_weight = None
        cleaned.append({"name": name, "focus": focus, "initial_weight": initial_weight})
    return cleaned


def _assess_research_coverage(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    draft: ScorecardDraft,
    conversation_context: str,
    findings: list[ResearchFinding],
    categories_covered: list[dict[str, Any]],
    rounds_so_far: int,
    sizing_hint: str | None = None,
) -> CoverageAssessment:
    """One synchronous Bedrock call (run via `asyncio.to_thread` by the caller, mirrors
    `_decide_categories`'s own shape) — the confidence-check mechanism behind the
    multi-round research loop (see `research_kpis` and `MAX_RESEARCH_ROUNDS`'s own
    docstring). A REAL judgment: the model is shown the actual consolidated findings and
    the categorized KPIs merged so far and asked to decide, grounded in that real content,
    whether a genuine gap remains — never a hardcoded heuristic, a coin flip, or an
    always-true/always-false stub.

    Fails SAFE: any malformed tool response, wrong tool name, or exception is treated as
    `sufficient=True` (see `CoverageAssessment`'s own docstring) — so a Bedrock hiccup on
    this one call degrades to "proceed with what we have", never an infinite or stuck
    research loop."""
    findings_summary = _format_research_findings_for_prompt([asdict(f) for f in findings]) or "(no findings yet)"
    current_kpi_names = ", ".join(
        f'{kpi.name} (under "{kpi.parent_name}")' if kpi.parent_name else kpi.name for kpi in draft.kpis
    ) or "(none yet)"
    categories_text = (
        "; ".join(f'"{c["name"]}" (focus: {c["focus"]})' for c in categories_covered) or "(none)"
    )
    system_prompt = _ASSESS_COVERAGE_SYSTEM_PROMPT.format(
        rounds_so_far=rounds_so_far,
        conversation_context=conversation_context or "(nothing yet)",
        categories_covered=categories_text,
        findings_summary=f"{findings_summary}\n\nKPIs merged so far: {current_kpi_names}",
        sizing_hint=f"\n{sizing_hint}\n" if sizing_hint else "",
    )
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": "Assess research coverage for this scorecard."}]}],
        system=system_prompt,
        tools=[ASSESS_RESEARCH_COVERAGE_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_name != "assess_research_coverage":
        logger.warning(
            "_assess_research_coverage: no usable assess_research_coverage tool call "
            "(stop_reason=%r); treating as sufficient (fail-safe).",
            getattr(result, "stop_reason", None),
        )
        return CoverageAssessment(sufficient=True, reasoning="(no usable assessment returned)", next_categories=[])

    data = result.tool_input or {}
    next_categories = _clean_category_items(data.get("next_categories") or [])
    # sufficient=True OR no usable next_categories both mean "stop" — an "insufficient"
    # verdict with nothing concrete to research next is functionally the same as
    # "sufficient" here.
    sufficient = bool(data.get("sufficient", True)) or not next_categories
    return CoverageAssessment(
        sufficient=sufficient, reasoning=str(data.get("reasoning") or ""), next_categories=next_categories
    )


# --- Bounded Bedrock concurrency -------------------------------------------------------------
#
# The research fan-out (agents x continuation chunks x dedup judging) can put dozens of Converse
# calls in flight for a 40+ KPI request. A process-wide semaphore (per event loop - an
# asyncio.Semaphore binds to the loop it is first awaited on, and tests run one loop per test)
# caps that at `settings.bedrock_max_concurrency` to avoid Bedrock throttling. Bedrock's quota is
# account-level, so with several worker processes the same cap is ALSO enforced cluster-wide through
# Redis (`bedrock_global_concurrency`, default = the per-process cap); without Redis, or if it is down,
# only the per-process cap applies.


def _bedrock_slots():
    """Async context manager gating one Bedrock call (per-process cap + cluster-wide cap)."""
    sem = get_semaphore(
        "bedrock",
        local_limit=lambda: get_settings().bedrock_max_concurrency,
        global_limit=lambda: get_settings().bedrock_global_concurrency or get_settings().bedrock_max_concurrency,
    )
    return sem.slot()


async def _converse_limited(bedrock: BedrockClientProtocol, **kwargs: Any) -> Any:
    """`bedrock.converse(**kwargs)` on a worker thread, gated by the shared concurrency cap."""
    async with _bedrock_slots():
        return await asyncio.to_thread(bedrock.converse, **kwargs)


# --- Fan-out sizing from a requested KPI count -------------------------------------------------


@dataclass(frozen=True)
class FanoutPlan:
    """How big the open-ended fan-out should be. All None = dynamic (no count requested): the
    model decides the category count from the domain's breadth and each agent keeps proposing
    chunks while it reports more distinct KPIs."""

    target: int | None = None
    categories: int | None = None
    per_category: int | None = None


def _plan_fanout(target: int | None) -> FanoutPlan:
    """`target` = how many RESEARCHED leaf KPIs are wanted. Categories ~ target / 6; each
    agent is asked for TARGET_OVERSHOOT x its share because dedup always loses a few (the
    reconcile step then trims any overshoot, so over-asking is cheap and under-asking is not)."""
    if not target or target <= 0:
        return FanoutPlan()
    categories = max(2, min(MAX_CATEGORIES, round(target / KPIS_PER_CATEGORY_TARGET)))
    return FanoutPlan(target, categories, math.ceil(target * TARGET_OVERSHOOT / categories))


def _dynamic_coverage_hint(leaves: int, categories: int) -> str:
    """Coverage hint when no KPI count was requested: ties the 'is research sufficient' judgement to the
    total leaf count so an accidentally thin result (a category lost to an error, few KPIs per
    category) triggers the deepen round."""
    return (
        f"COVERAGE NOTE: so far {leaves} distinct KPIs exist across {categories} categories. A scorecard for a "
        "broad operational domain normally needs roughly 25-40; under about 15 means coverage is probably "
        "INSUFFICIENT (a category may have failed or been shallow) unless the domain is genuinely narrow — then "
        "name the categories to deepen or add."
    )


def _sizing_hint(plan: FanoutPlan, *, have: int | None = None) -> str | None:
    if plan.target is None:
        return None
    text = (
        f"SIZING NOTE: the user asked for about {plan.target} KPIs in total. Plan about {plan.categories} "
        f"categories (more if the domain genuinely needs them) so each can carry roughly {plan.per_category} "
        "distinct, genuinely valuable KPIs. Never pad with weak or overlapping KPIs: if the domain cannot "
        "support that many distinct ones, plan fewer."
    )
    if have is not None:
        text += (
            f" So far only {have} distinct KPIs exist; unless the domain is truly exhausted, treat coverage as "
            "INSUFFICIENT and name new categories or existing ones to deepen."
        )
    return text


# --- KPI-name duplicate detection ---------------------------------------------------------------
#
# The old rule (SequenceMatcher >= 0.82 OR one name a substring of the other) silently dropped
# real, distinct KPIs: "Defect Escape Rate" vs "Defect Escape Rate by Severity", "Test Coverage" vs
# "Test Coverage of Critical Paths", "Customer Satisfaction Score" vs "... Trend" (measured:
# ratios 0.75 / 0.59 / 0.89). Now three tiers: SAME (normalized-equal, equal token sets after
# singularizing/stopwords, or ratio >= 0.9) is dropped deterministically; MAYBE (token subset with
# extra words, token Jaccard >= 0.6, or ratio >= 0.72) goes to ONE batched judge call on the small
# model (`_judge_duplicate_pairs`) and is kept unless the judge says it is the same concept —
# and also kept if the judge is unavailable (never drop a KPI on uncertainty). Embeddings were
# considered (embeddings.py) but a similarity cut-off for short KPI names could not be validated
# offline, whereas a judge call is threshold-free.

_NAME_STOPWORDS = frozenset({"of", "the", "a", "an", "and", "for", "to", "in", "on", "by", "per", "vs", "with"})


def _normalize_kpi_name_for_dedup(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _name_tokens(norm: str) -> frozenset[str]:
    return frozenset(
        (t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t)
        for t in norm.split()
        if t not in _NAME_STOPWORDS
    )


def _dup_relation(norm: str, other: str) -> str:
    """'same' | 'maybe' | 'distinct' for two normalized KPI names."""
    if norm == other:
        return "same"
    ta, tb = _name_tokens(norm), _name_tokens(other)
    if ta and ta == tb:
        return "same"
    union = ta | tb
    jaccard = len(ta & tb) / len(union) if union else 0.0
    token_overlap = bool((ta and tb and (ta < tb or tb < ta)) or jaccard >= 0.6)
    matcher = difflib.SequenceMatcher(None, norm, other)
    # quick_ratio() is a cheap upper bound on ratio(): most pairs are clearly distinct, and the
    # full ratio is O(n^2) per pair (this runs over every pair in a 100+ KPI pool).
    if matcher.quick_ratio() < 0.72:
        return "maybe" if token_overlap else "distinct"
    ratio = matcher.ratio()
    if ratio >= 0.9:
        return "same"
    return "maybe" if token_overlap or ratio >= 0.72 else "distinct"


def _is_near_duplicate(
    norm: str,
    existing_norms: list[str],
    *,
    maybe_counts: bool = True,
    same_pairs: set[frozenset[str]] | None = None,
) -> bool:
    """The single near-duplicate rule shared by `_dedupe_kpi_batch_items` and the pinned-KPI
    filter in `_merge_research_kpi_batches` (see the tier comment above). `maybe_counts=True`
    treats MAYBE as a duplicate (pinned filter: the user's own KPI always wins); otherwise a
    MAYBE pair is a duplicate only if the judge confirmed it (`same_pairs`)."""
    for existing in existing_norms:
        relation = _dup_relation(norm, existing)
        if relation == "same":
            return True
        if relation == "maybe" and (
            maybe_counts or (same_pairs is not None and frozenset((norm, existing)) in same_pairs)
        ):
            return True
    return False


def _borderline_pairs(names: list[str]) -> list[tuple[str, str]]:
    """Pairs of (normalized) names in the MAYBE tier, each once, order-insensitive."""
    norms = list(dict.fromkeys(n for n in (_normalize_kpi_name_for_dedup(x) for x in names) if n))
    pairs: list[tuple[str, str]] = []
    for i, a in enumerate(norms):
        for b in norms[i + 1 :]:
            if _dup_relation(a, b) == "maybe":
                pairs.append((a, b))
    return pairs


JUDGE_DUPLICATE_KPIS_TOOL = ToolSpec(
    name="judge_duplicate_kpis",
    description=(
        "For each numbered pair of KPI names, decide whether the two are the SAME measurement concept "
        "(one could replace the other with no loss) or genuinely DISTINCT measurements that merely share "
        "words (e.g. 'Defect Escape Rate' vs 'Defect Escape Rate by Severity' are usually distinct "
        "views; 'Response Time' vs 'Response Time Speed' are the same). Return only the ids that are the "
        "SAME concept."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "same_concept_ids": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["same_concept_ids"],
    },
)
DUPLICATE_JUDGE_CHUNK = 60


async def _judge_duplicate_pairs(
    bedrock: BedrockClientProtocol,
    pairs: list[tuple[str, str]],
    cache: dict[frozenset[str], bool],
) -> None:
    """Fills `cache` ({normalized pair: is-same-concept}) for every not-yet-judged pair, in
    concurrent chunks on the small judge model. A failed/unusable chunk leaves its pairs OUT of
    the cache (= treated as distinct: keep both). Never raises."""
    todo = [p for p in pairs if frozenset(p) not in cache]
    chunks = [todo[i : i + DUPLICATE_JUDGE_CHUNK] for i in range(0, len(todo), DUPLICATE_JUDGE_CHUNK)]

    async def judge(chunk: list[tuple[str, str]]) -> None:
        listing = "\n".join(f'{i}. "{a}"  <->  "{b}"' for i, (a, b) in enumerate(chunk))
        try:
            result = await _converse_limited(
                bedrock,
                messages=[{"role": "user", "content": [{"text": f"Judge these KPI name pairs:\n{listing}"}]}],
                system="You decide whether two KPI names are the same measurement concept. Call the tool once.",
                tools=[JUDGE_DUPLICATE_KPIS_TOOL],
                force_tool_use=True,
                model_id=get_settings().bedrock_judge_model_id,
            )
            if not result.is_tool_use or result.tool_name != "judge_duplicate_kpis" or result.truncated:
                return
            same = {
                int(i) for i in (result.tool_input or {}).get("same_concept_ids") or [] if isinstance(i, int | float)
            }
            for i, pair in enumerate(chunk):
                cache[frozenset(pair)] = i in same
        except Exception:  # noqa: BLE001 — judging is an optimization; never break the turn
            logger.warning("duplicate-KPI judge call failed; keeping the borderline pairs.", exc_info=True)

    await asyncio.gather(*(judge(c) for c in chunks))


# --- Chunked KPI proposal (one category) ----------------------------------------------------------


async def _propose_category_kpis(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    *,
    category: str,
    focus: str,
    finding: ResearchFinding,
    avoid_names: list[str] | None,
    want: int | None,
    avoid_is_user: bool = True,
    conversation_context: str = "",
    session_id: str | None = None,
    turn_started_at: datetime | None = None,
    actor: str = "master",
) -> list[dict[str, Any]]:
    """Asks for one category's KPIs in bounded `propose_kpi_batch` chunks (<= `quota` per call,
    so no single tool call has to carry dozens of 11-level rubrics). Keeps asking while the agent
    reports `has_more` (or, when `want` is given, until `want` is reached unless it explicitly
    says it is exhausted), stopping early when a call adds nothing new (never loops on
    padding) or after MAX_BATCH_CALLS_PER_AGENT. A call that hits the output-token limit
    (`ConverseResult.truncated` — its tool input is incomplete) is retried with half the quota
    instead of being trusted. A Bedrock exception propagates only if nothing was collected yet (the
    caller treats that as "this category proposed no KPIs" but keeps the finding); a failed
    continuation keeps what was already collected.

    The proposal calls carry only name/weight/rationale (small, fast, never truncated); the 0-10
    rubrics are then written for ALL collected KPIs at once in parallel compact chunks
    (`_fill_missing_guidelines`), so a KPI can never leave this function with a partial rubric."""
    collected: list[dict[str, Any]] = []
    rationales: dict[str, str] = {}
    seen_norms = [_normalize_kpi_name_for_dedup(n) for n in avoid_names or []]
    quota = KPIS_PER_BATCH_CALL
    for _ in range(MAX_BATCH_CALLS_PER_AGENT):
        if len(collected) >= MAX_KPIS_PER_CATEGORY:
            break
        ask = quota if want is None else max(1, min(quota, want - len(collected)))
        quota_note = (
            f" Propose AT MOST {ask} KPIs in this call; if more distinct, genuinely "
            "valuable KPIs remain for this category, set `has_more=true` and you will be asked for the next "
            "chunk. Never pad with weak, overlapping or restated KPIs — fewer is correct when the category is "
            "exhausted."
        )
        if want is not None:
            quota_note += f" The scorecard needs about {want} KPIs from this category in total."
        else:
            quota_note += (
                " Your research supports roughly 5-8 distinct, valuable KPIs for this category: propose that "
                "many (fewer only if the category truly has fewer; set `has_more=true` if more remain)."
            )
        system_prompt = _PROPOSE_KPI_BATCH_SYSTEM_PROMPT_TEMPLATE.format(
            category=category,
            focus=focus,
            summary=finding.summary or "(none)",
            suggested_kpis=json.dumps(finding.suggested_kpis),
            suggested_thresholds=json.dumps(finding.suggested_thresholds),
            sources=json.dumps(finding.sources),
            quota_note=quota_note,
        )
        if avoid_names and avoid_is_user:
            system_prompt += (
                "\n\nThe user has ALREADY defined these KPIs themselves — do NOT propose them "
                "or near-duplicates of them; propose only COMPLEMENTARY KPIs for your category: "
                + "; ".join(f'"{n}"' for n in avoid_names)
            )
        elif avoid_names:
            system_prompt += (
                "\n\nThe scorecard ALREADY contains these KPIs — do NOT repeat or restate any of them; propose "
                "only further DISTINCT, genuinely valuable KPIs for your category: "
                + "; ".join(f'"{n}"' for n in avoid_names)
            )
        if collected:
            system_prompt += (
                "\n\nYou ALREADY proposed these for your category — do NOT repeat or restate them, propose only "
                "further DISTINCT KPIs: " + "; ".join(f'"{k["name"]}"' for k in collected)
            )
        try:
            result = await _converse_limited(
                bedrock,
                messages=[{"role": "user", "content": [{"text": "Propose your KPI batch now."}]}],
                system=system_prompt,
                tools=[PROPOSE_KPI_BATCH_TOOL],
                force_tool_use=True,
                model_id=model_id,
            )
        except Exception:
            if not collected:
                raise
            logger.warning("propose_kpi_batch continuation for %r failed; keeping %d KPIs.", category, len(collected))
            break
        if result.truncated:
            if quota <= 2:
                logger.warning("propose_kpi_batch for %r still truncated at quota 2; giving up on more.", category)
                break
            quota = max(2, quota // 2)
            logger.warning(
                "propose_kpi_batch for %r hit the output-token limit; retrying with quota %d.", category, quota
            )
            continue
        if not result.is_tool_use or result.tool_name != "propose_kpi_batch":
            logger.warning(
                "research agent category=%r: propose_kpi_batch did not return a usable tool call (stop_reason=%r).",
                category,
                getattr(result, "stop_reason", None),
            )
            break
        data = result.tool_input or {}
        items = _validate_kpi_batch_items(data.get("kpis") or [], category)
        for raw_item in data.get("kpis") or []:
            if isinstance(raw_item, dict) and raw_item.get("name") and raw_item.get("rationale"):
                rationales[str(raw_item["name"])] = str(raw_item["rationale"])
        fresh: list[dict[str, Any]] = []
        for item in items:
            norm = _normalize_kpi_name_for_dedup(str(item.get("name") or ""))
            if norm and not _is_near_duplicate(norm, seen_norms, maybe_counts=False):
                fresh.append(item)
                seen_norms.append(norm)
        collected.extend(fresh)
        if not fresh:
            break
        has_more = data.get("has_more")
        if want is None:
            if has_more is not True:
                break
        elif len(collected) >= want or has_more is False:
            break
    collected = collected[:MAX_KPIS_PER_CATEGORY]
    need = [k for k in collected if not _complete_guidelines(k.get("guidelines") or {})]
    if need:
        if session_id is not None:
            await emit_turn_event(
                session_id, turn_started_at, actor, "guidelines",
                f'Writing 0-10 guidelines for {len(need)} KPI(s) of "{category}" in parallel…',
            )
        started = time.monotonic()
        fill_stats: dict[str, int] = {}
        filled, fallback = await _fill_missing_guidelines(
            [
                {"name": k["name"], "parent_name": category, "guidance": rationales.get(k["name"], "")}
                for k in need
            ],
            bedrock, model_id, conversation_context or "(nothing yet)", [finding] if finding.has_content() else None,
            stats=fill_stats,
        )
        if fill_stats.get("no_numeric") and session_id is not None:
            await emit_turn_event(
                session_id, turn_started_at, actor, "guidelines",
                f'{fill_stats["no_numeric"]} of {fill_stats["total"]} KPI(s) of "{category}" have no numeric '
                "thresholds: their top level is marked as a proposed target to validate.",
            )
        for k in need:
            k["guidelines"] = filled[k["name"]]
        if fallback:
            logger.warning("category %r: %d KPI(s) got the fallback rubric: %s", category, len(fallback), fallback)
        logger.info("category %r: guidelines for %d KPI(s) took %.1fs", category, len(need), time.monotonic() - started)
    return collected


def _finding_to_gate_text(finding: ResearchFinding) -> str:
    """Renders one research agent's recorded finding as plain text for quality-gate
    checkpoint 2's `answer` (see `_run_research_agent`) — everything the finding actually
    contributes (summary, suggested KPIs/thresholds/sources), not just the bare
    `summary` field, since that's what genuinely represents "what this agent produced"."""
    lines = [finding.summary or "(no summary)"]
    for kpi in finding.suggested_kpis:
        lines.append(f"- Suggested KPI: {kpi.get('name', '')} — {kpi.get('rationale', '')}")
    for threshold in finding.suggested_thresholds:
        lines.append(f"- Suggested threshold: {threshold.get('metric', '')} = {threshold.get('value_or_range', '')}")
    for source in finding.sources:
        lines.append(f"- Source: {source.get('title', '')} ({source.get('url', '')})")
    return "\n".join(lines)


async def _run_research_agent(
    category: str,
    focus: str,
    bedrock: BedrockClientProtocol,
    web_search_client: WebSearchClientProtocol,
    model_id: str | None,
    *,
    session_id: str,
    turn_started_at: datetime | None,
    actor: str,
    round_num: int = 1,
    jev_client: JevClientProtocol | None = None,
    conversation_context: str = "",
    propose_batch: bool = True,
    avoid_kpi_names: list[str] | None = None,
    want_kpis: int | None = None,
    max_searches: int = MAX_SEARCH_CALLS_PER_RESEARCH_AGENT,
    use_quality_gate: bool = True,
) -> ResearchFinding:
    """THE single research-agent definition (instantiated/invoked N times concurrently by
    `research_kpis` via `asyncio.gather`, never duplicated in code) — a bounded ReAct-style
    worker: LLM + web_search, capped at MAX_SEARCH_CALLS_PER_RESEARCH_AGENT search calls
    then forced to close with `record_research_finding`, mirroring propose_kpis's own
    bounded web_search loop (see MAX_WEB_SEARCH_CALLS_PER_PROPOSE). One invocation = one
    CATEGORY's dedicated research assignment (see the module comment above `MAX_CATEGORIES`)
    — `category`/`focus` here are exactly one `decide_categories`/`assess_research_coverage`
    item's `name`/`focus`.

    `actor` (e.g. `"research_agent_2"`, assigned index-based by the caller WITHIN its
    round — see `research_kpis`) is this specific concurrent invocation's stable identifier
    for the live-trace event log (see `app/ai/turn_events.py`): every event this worker
    emits is tagged with it, so a reader can tell which of the N concurrently-running
    agents produced which event even though they interleave in `created_at` order — and,
    since every event's own message text names the category too (e.g. 'Researching
    "Schedule": ...'), reading `chat_turn_events` directly proves "one agent per category"
    end to end. `round_num` (default 1) additionally tags every event with which research
    ROUND this invocation belongs to (see `MAX_RESEARCH_ROUNDS`).

    Contract: NEVER raises — mirrors web_search.py's own "return [], never raise" contract
    (see that module's docstring) one level up. Any failure anywhere in this worker (a
    Bedrock call, a malformed response, an unexpected web_search exception) is caught here
    and turned into a thin `degraded=True` finding, so one failing category can never crash
    the whole turn or the other concurrently-running agents.

    `jev_client`/`conversation_context` power quality-gate checkpoint 2 (see the module
    comment above `MAX_QUALITY_GATE_RETRIES`): once `record_research_finding` produces a
    finding, Jev rates how well it matches THIS agent's assigned category/focus
    (instruction=`conversation_context` + category/focus, answer=the finding). Below
    threshold, the agent researches further and re-synthesizes — bounded at
    `MAX_QUALITY_GATE_RETRIES` attempts, sharing the SAME `MAX_SEARCH_CALLS_PER_RESEARCH_
    AGENT` web_search budget across every attempt (not reset per retry) — then returns its
    best-scoring finding rather than hanging or dropping the category.

    `propose_batch=False` (user-specified KPI mode — see `_enrich_user_kpis`) stops after the
    recorded finding: the KPIs are already decided by the user, so this agent only
    researches benchmarks/thresholds/weighting for them and never invents KPIs of its own.
    `avoid_kpi_names` (hybrid mode) lists the user's pinned KPIs, which this agent's
    own batch must complement rather than duplicate.

    `want_kpis` (a requested KPI count — see `_plan_fanout`): how many KPIs this category should
    contribute; the agent proposes them in bounded chunks (`_propose_category_kpis`). `None` =
    dynamic. `max_searches=0` runs the agent from the model's own knowledge (no web search
    configured, large requested count)."""
    try:
        await emit_turn_event(
            session_id, turn_started_at, actor, "started", f'Researching "{category}": {focus}', round=round_num
        )
        local_messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [{"text": f"Category: {category}\nFocus: {focus}"}],
            }
        ]
        search_calls_made = 0
        terse_retry = False
        finding: ResearchFinding | None = None
        last_result = None
        best_finding: ResearchFinding | None = None
        best_score = -1.0
        revision_feedback: str | None = None
        # Tracks the PREVIOUS attempt's rendered finding text, so a second-failure critique
        # can see what actually changed in response to the first critique (see
        # `_generate_quality_gate_critique`'s own docstring).
        previous_finding_text: str | None = None

        # Outer loop = quality-gate checkpoint 2's bounded retry (see the module comment
        # above MAX_QUALITY_GATE_RETRIES); inner `while True` = the UNCHANGED bounded
        # search-then-record loop, reused as-is on every gate attempt (search budget is
        # shared/not reset across attempts — see this function's own docstring).
        for gate_attempt in range(MAX_QUALITY_GATE_RETRIES + 1):
            result = None
            while True:
                budget_left = search_calls_made < max_searches
                tools = (
                    [WEB_SEARCH_TOOL, RECORD_RESEARCH_FINDING_TOOL]
                    if budget_left
                    else [RECORD_RESEARCH_FINDING_TOOL]
                )
                system_prompt = _RESEARCH_WORKER_SYSTEM_PROMPT_TEMPLATE.format(
                    category=category, focus=focus, max_calls=max_searches
                )
                if not budget_left:
                    system_prompt += (
                        "\n\nYour search budget is used up — call record_research_finding now "
                        "with whatever you have (even if you never searched)."
                    )
                    # This call's tools=[RECORD_RESEARCH_FINDING_TOOL] with force_tool_use=True
                    # means it is guaranteed to resolve to record_research_finding — safe to
                    # report "synthesizing" as a real live signal *before* the (possibly
                    # several-second) Bedrock call returns, not just after the fact.
                    await emit_turn_event(
                        session_id, turn_started_at, actor, "synthesizing",
                        f'Synthesizing findings for "{category}"…',
                        round=round_num,
                    )
                if revision_feedback:
                    # Only set on a quality-gate retry (gate_attempt > 0) — see below.
                    system_prompt += f"\n\n{revision_feedback}"

                if terse_retry:
                    system_prompt += (
                        "\n\nYour previous answer was CUT OFF. Be brief: summary under 120 words, at most 5 "
                        "suggested KPIs, at most 4 thresholds and 3 sources."
                    )
                result = await _converse_limited(
                    bedrock,
                    messages=local_messages,
                    system=system_prompt,
                    tools=tools,
                    force_tool_use=True,
                    model_id=model_id,
                )
                if result.truncated and not terse_retry:
                    terse_retry = True
                    logger.warning("research agent category=%r: output truncated; retrying tersely.", category)
                    continue

                if result.is_tool_use and result.tool_name == "web_search" and budget_left:
                    query = str((result.tool_input or {}).get("query") or "").strip()
                    search_calls_made += 1
                    await emit_turn_event(
                        session_id, turn_started_at, actor, "searching", f'Searching: "{query}"', round=round_num
                    )
                    logger.info(
                        "research agent category=%r: web_search (%d/%d) query=%r",
                        category,
                        search_calls_made,
                        max_searches,
                        query,
                    )
                    try:
                        results = await web_search_client.search(query) if query else []
                    except Exception:  # noqa: BLE001 — see web_search.py's own "never raise" contract
                        logger.warning(
                            "research agent category=%r: web_search raised unexpectedly; treating as no results.",
                            category,
                            exc_info=True,
                        )
                        results = []
                    await emit_turn_event(
                        session_id, turn_started_at, actor, "search_result",
                        f'Found {len(results)} result(s) for "{query}"',
                        round=round_num,
                    )
                    logger.info(
                        "research agent category=%r: web_search query=%r -> %d result(s)%s",
                        category,
                        query,
                        len(results),
                        f"; first={results[0].title!r} ({results[0].url})" if results else "",
                    )
                    local_messages.append(
                        {"role": "assistant", "content": [{"text": f"[called web_search] query={query!r}"}]}
                    )
                    local_messages.append(
                        {"role": "user", "content": [{"text": _format_search_results(query, results)}]}
                    )
                    continue

                break

            last_result = result

            if not (result is not None and result.is_tool_use and result.tool_name == "record_research_finding"):
                # Model never produced a usable finding this attempt — nothing to quality-
                # gate; fall through to the degraded-return path below exactly as before
                # this feature existed (this failure mode is not retried by the gate).
                break

            data = result.tool_input or {}
            candidate = ResearchFinding(
                category=category,
                summary=str(data.get("summary") or ""),
                suggested_kpis=[k for k in (data.get("suggested_kpis") or []) if isinstance(k, dict)],
                suggested_thresholds=[
                    t for t in (data.get("suggested_thresholds") or []) if isinstance(t, dict)
                ],
                sources=[s for s in (data.get("sources") or []) if isinstance(s, dict)],
            )

            if not use_quality_gate:
                # User-specified/hybrid enrichment & weighting: the agent only gathers benchmarks for
                # KPIs the user already chose. The Jev gate (5-80s per call, usually followed by a
                # re-synthesis) is for open-ended research and only burns minutes here.
                finding = candidate
                break

            # --- Quality-gate checkpoint 2 (see the module comment above
            # MAX_QUALITY_GATE_RETRIES): rate how well this finding matches what this
            # agent was actually assigned to research.
            gate_instruction = (
                f"User's original request: {conversation_context or '(nothing yet)'}\n"
                f'This research agent\'s assigned category: "{category}"\n'
                f'Specific research focus: "{focus}"'
            )
            finding_text = _finding_to_gate_text(candidate)
            gate = await _gate(
                jev_client, instruction=gate_instruction, answer=finding_text, turn_started_at=turn_started_at
            )

            if gate.degraded:
                await emit_turn_event(
                    session_id, turn_started_at, actor, "quality_gate",
                    f'"{category}": finding quality check — Jev was unreachable — gate treated as '
                    "passed (graceful degradation).",
                    round=round_num,
                )
                finding = candidate  # Jev unreachable — gate passed by policy; finding is final.
                break

            assert gate.score is not None  # guaranteed whenever degraded=False
            if gate.score > best_score:
                best_finding, best_score = candidate, gate.score

            if gate.passed:
                finding = candidate
                await emit_turn_event(
                    session_id, turn_started_at, actor, "quality_gate",
                    f'"{category}": finding quality check scored {gate.score:.2f} '
                    f"(>= {QUALITY_GATE_THRESHOLD}) — "
                    + ("passed after revision." if gate_attempt > 0 else "passed."),
                    round=round_num,
                )
                break

            if gate_attempt < MAX_QUALITY_GATE_RETRIES:
                await emit_turn_event(
                    session_id, turn_started_at, actor, "quality_gate_retry",
                    f'"{category}": finding quality check scored {gate.score:.2f} '
                    f"(below {QUALITY_GATE_THRESHOLD}) — researching further…",
                    round=round_num,
                )
                local_messages.append(
                    {"role": "assistant", "content": [{"text": f"[recorded finding] {json.dumps(data)}"}]}
                )
                # Advisor/critique step (see the module comment above
                # MAX_QUALITY_GATE_RETRIES): a concrete, specific critique of THIS finding
                # in place of a generic "research further" note — on the second failure,
                # also carries what the first critique suggested and what actually changed.
                revision_feedback = await _generate_quality_gate_critique(
                    bedrock, model_id,
                    task_context=gate_instruction,
                    produced_output=finding_text,
                    gate_score=gate.score,
                    previous_critique=revision_feedback,
                    previous_output=previous_finding_text,
                )
                previous_finding_text = finding_text
                local_messages.append({"role": "user", "content": [{"text": revision_feedback}]})
                continue

            # Bounded cap reached and still below threshold — proceed with the best-scoring
            # attempt seen across every gate_attempt (never hang, never silently drop it).
            finding = best_finding if best_finding is not None else candidate
            await emit_turn_event(
                session_id, turn_started_at, actor, "quality_gate",
                f'"{category}": finding quality check still below {QUALITY_GATE_THRESHOLD} after '
                f"{MAX_QUALITY_GATE_RETRIES} revision(s) (best score {best_score:.2f}) — "
                "using the best attempt.",
                round=round_num,
            )
            break

        if finding is not None and not propose_batch:
            await emit_turn_event(
                session_id, turn_started_at, actor, "completed",
                f'Finished researching "{category}": {len(finding.suggested_thresholds)} threshold(s), '
                f"{len(finding.sources)} source(s).",
                round=round_num,
            )
            return finding

        if finding is not None:
            # --- KPI batch proposal: ONE additional bounded tool call, grounded in the
            # finding this same agent just recorded (post-quality-gate), proposing this
            # category's own small batch of fully-specified KPIs. Never lets a failure here
            # lose the finding itself (finding.proposed_kpis simply stays [] — the finding
            # above is already fully formed and returned regardless).
            await emit_turn_event(
                session_id, turn_started_at, actor, "proposing_kpis", f'Proposing KPIs for "{category}"…',
                round=round_num,
            )
            try:
                finding.proposed_kpis = await _propose_category_kpis(
                    bedrock, model_id,
                    category=category, focus=focus, finding=finding,
                    avoid_names=avoid_kpi_names, want=want_kpis,
                    conversation_context=conversation_context, session_id=session_id,
                    turn_started_at=turn_started_at, actor=actor,
                )
            except Exception:  # noqa: BLE001 — a failed KPI-batch call must never lose the finding itself
                logger.warning("research agent category=%r: propose_kpi_batch call failed.", category, exc_info=True)

            if finding.proposed_kpis:
                names = ", ".join(k["name"] for k in finding.proposed_kpis)
                await emit_turn_event(
                    session_id, turn_started_at, actor, "proposed_kpis",
                    f'Proposed {len(finding.proposed_kpis)} KPI(s) for "{category}": {names}',
                    round=round_num,
                )
            else:
                await emit_turn_event(
                    session_id, turn_started_at, actor, "proposed_kpis",
                    f'"{category}": no KPIs proposed from this category.',
                    round=round_num,
                )

            await emit_turn_event(
                session_id, turn_started_at, actor, "completed",
                f'Finished "{category}": {len(finding.proposed_kpis)} KPI(s) proposed, '
                f"{len(finding.suggested_thresholds)} threshold(s), {len(finding.sources)} source(s).",
                round=round_num,
            )
            return finding

        logger.warning(
            "research agent category=%r: model did not call record_research_finding "
            "(stop_reason=%r); returning a degraded finding.",
            category,
            getattr(last_result, "stop_reason", None),
        )
        await emit_turn_event(
            session_id, turn_started_at, actor, "error",
            f'"{category}": model did not produce a usable finding — skipping this category.',
            round=round_num,
        )
        return ResearchFinding(
            category=category, summary=(last_result.text if last_result else "") or "", degraded=True
        )
    except Exception:  # noqa: BLE001 — this worker's whole-agent "never raise" contract; see docstring
        logger.warning(
            "research agent category=%r failed entirely; returning a degraded finding.", category, exc_info=True
        )
        await emit_turn_event(
            session_id, turn_started_at, actor, "error",
            f'"{category}": research agent failed — skipping this category.',
            round=round_num,
        )
        return ResearchFinding(category=category, degraded=True)


def _format_research_findings_for_prompt(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return ""
    lines = [
        f"Grounding research from {len(findings)} independently-researched categor"
        + ("y" if len(findings) == 1 else "ies")
        + " — weave these real findings, and ESPECIALLY their suggested_thresholds/sources, "
        "into your KPI proposal and quantitative_criteria instead of inventing "
        "plausible-sounding numbers:"
    ]
    for f in findings:
        lines.append(f"\n## Category: {f.get('category', '(unknown)')}")
        if f.get("summary"):
            lines.append(f"Summary: {f['summary']}")
        for kpi in f.get("suggested_kpis") or []:
            lines.append(f"- Suggested KPI: {kpi.get('name', '')} — {kpi.get('rationale', '')}")
        for threshold in f.get("suggested_thresholds") or []:
            note = f" ({threshold.get('source_note')})" if threshold.get("source_note") else ""
            lines.append(
                f"- Suggested threshold: {threshold.get('metric', '')} = {threshold.get('value_or_range', '')}{note}"
            )
        for source in f.get("sources") or []:
            lines.append(f"- Source: {source.get('title', '')} ({source.get('url', '')})")
    return "\n".join(lines)


def _normalize_guideline_value(raw: Any) -> Any:
    """Defensive repair for a real, observed GLM-5 tool-call adherence gap: despite
    `PROPOSE_KPI_BATCH_TOOL`'s (and `UPDATE_DRAFT_TOOL`'s) schema explicitly requiring each
    guideline rung's value to be `{qualitative_text (str), quantitative_criteria (object |
    null)}`, the model sometimes instead returns the rung's value as a BARE STRING (just
    the qualitative description, unnested) — confirmed live on 2026-09-30, where a real
    `propose_kpi_batch` call returned exactly this shape for 3 whole KPI candidates in one
    batch. `KpiDraft.model_validate` correctly rejects that shape (`GuidelineDraft` needs a
    dict), but the ORIGINAL handling of that rejection (`_validate_kpi_batch_items`, below)
    just logged-and-dropped the whole KPI candidate, silently losing real, usable content
    over a nesting mistake alone. A bare string IS valid qualitative text — wrap it as
    `{"qualitative_text": <string>, "quantitative_criteria": None}` instead of discarding
    it, recovering the candidate rather than losing it to a shape technicality."""
    if isinstance(raw, str):
        return {"qualitative_text": raw, "quantitative_criteria": None}
    return raw


def _normalize_kpi_guidelines(candidate: dict[str, Any]) -> dict[str, Any]:
    """Applies `_normalize_guideline_value` to every rung of one KPI candidate's
    `guidelines` dict, if present and shaped as a dict — a no-op for anything else (already
    correctly-shaped guidelines, a missing/malformed `guidelines` key, etc.), so this never
    masks a genuinely different validation problem."""
    guidelines = candidate.get("guidelines")
    if not isinstance(guidelines, dict):
        return candidate
    return {**candidate, "guidelines": {k: _normalize_guideline_value(v) for k, v in guidelines.items()}}


def _validate_kpi_batch_items(raw_items: list[Any], category_name: str) -> list[dict[str, Any]]:
    """Validates each raw `propose_kpi_batch` item against `KpiDraft` (weight bounds,
    guideline score-level keys, etc — the same schema `update_draft` patches are validated
    against) — an invalid item is dropped and logged, never allowed to reach the merge step
    or corrupt draft state.

    Every item is forced to `level=2, parent_name=category_name`: a research agent IS one
    category's dedicated research assignment (see the module comment above
    `MAX_CATEGORIES`), so every KPI it proposes unambiguously belongs under that category —
    there is no longer any "flat, unparented" KPI shape coming out of the research fan-out
    (contrast with this function's pre-category behavior, which forced `level=1,
    parent_name=None` since an "angle" had no defined place in any hierarchy). The
    category itself becomes its own `level=1, parent_name=None` KpiDraft — see
    `_merge_research_kpi_batches`, which builds that node once per surviving category
    rather than here (this function only ever sees ONE category's own KPI items).

    Guideline values are repaired via `_normalize_kpi_guidelines` BEFORE validation (see
    that function's own docstring for the real, observed failure mode it recovers from) —
    a candidate is only ever dropped now for a GENUINE validation failure (a real missing
    field, an out-of-range weight, an invalid score-level key, ...), not merely because the
    model nested a qualitative description one level too shallow."""
    validated: list[dict[str, Any]] = []
    for raw in raw_items[:MAX_KPIS_PER_CALL]:
        if not isinstance(raw, dict):
            continue
        candidate = _normalize_kpi_guidelines({**raw, "level": 2, "parent_name": category_name})
        try:
            kpi = KpiDraft.model_validate(candidate)
        except ValidationError:
            logger.warning("propose_kpi_batch: dropping an invalid KPI item %r", raw, exc_info=True)
            continue
        dumped = kpi.model_dump(mode="json")
        if not _complete_guidelines(dumped["guidelines"]):
            # A partial rubric (the model stopped after a few levels) must never reach the draft:
            # drop it so the guideline-writing step regenerates ALL 11 levels for this KPI.
            dumped["guidelines"] = {}
        validated.append(dumped)
    return validated


def _dedupe_kpi_batch_items(
    items: list[dict[str, Any]], same_pairs: set[frozenset[str]] | None = None
) -> list[dict[str, Any]]:
    """Name/concept-similarity dedup — now applied ACROSS every category's pooled batch at
    once (see `_merge_research_kpi_batches`), not per category in isolation, since the same
    KPI concept (e.g. "Response Time") can genuinely surface under two different categories'
    independent research. A pair is treated as a duplicate when their normalized names are
    either a close fuzzy match (`difflib.SequenceMatcher` ratio >= 0.82) or one is a
    substring of the other AND both are long enough (>= 8 normalized chars) for that
    substring relationship to be meaningful rather than a coincidence of short names —
    regardless of which category each item's `parent_name` says it belongs to.

    Resolution policy (simple and deterministic, documented here rather than left implicit):
    whichever occurrence appears FIRST in `items`' order wins and is kept; every later
    near-duplicate is dropped. The caller (`_merge_research_kpi_batches`) always passes
    `items` round-robin-interleaved across categories, so in practice this means "the
    category whose research agent proposed this concept earliest in the fair, round-robin
    ordering keeps it" — a reasonable, cheap proxy for "best-fitting category" without
    needing a second LLM call just to adjudicate duplicates."""
    kept: list[dict[str, Any]] = []
    kept_norms: list[str] = []
    for item in items:
        norm = _normalize_kpi_name_for_dedup(str(item.get("name") or ""))
        if not norm:
            continue
        if _is_near_duplicate(norm, kept_norms, maybe_counts=False, same_pairs=same_pairs):
            continue
        kept.append(item)
        kept_norms.append(norm)
    return kept


def _normalize_weights_to_100(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Proportionally rescales every `included_in_scoring` item's `weight` so the set sums
    to 100 — a mechanical starting point, not a qualitative judgment. Generic over WHICH
    group of LEAF items it's applied to: `_merge_research_kpi_batches` below calls this
    once per category (to renormalize that category's own children relative to each
    other) and once more across ALL surviving leaves together, across every category, to
    land the WHOLE scorecard's leaf weights on exactly 100 (categories themselves are
    never passed to this function — they carry no weight at all; see migration
    0008_category_nodes_no_weight) — the same helper either way. Mirrors
    `redistributeWeight`'s "remainder on the largest" rounding-drift fix in
    `frontend/lib/kpi-tree.ts`. Items with `included_in_scoring=False` are left untouched
    (not part of the sum-to-100 group at all — see migration
    0005_scoring_formula_and_kpi_flags)."""
    included = [it for it in items if it.get("included_in_scoring", True)]
    total = sum(float(it.get("weight") or 0) for it in included)
    if total <= 0:
        return items
    scale = 100.0 / total
    out: list[dict[str, Any]] = []
    for it in items:
        new_item = dict(it)
        if it.get("included_in_scoring", True):
            new_item["weight"] = round(float(it.get("weight") or 0) * scale, 2)
        out.append(new_item)
    included_out = [it for it in out if it.get("included_in_scoring", True)]
    drift = round(100.0 - sum(it["weight"] for it in included_out), 2)
    if included_out and abs(drift) >= 0.01:
        largest = max(included_out, key=lambda it: it["weight"])
        largest["weight"] = round(largest["weight"] + drift, 2)
    return out


def _merge_research_kpi_batches(
    findings: list[ResearchFinding],
    category_meta: dict[str, dict[str, Any]],
    pinned: list[dict[str, Any]] | None = None,
    same_pairs: set[frozenset[str]] | None = None,
) -> list[dict[str, Any]]:
    """THE merge point, now category/hierarchy-aware (see the module comment above
    `MAX_CATEGORIES`): every agent's own KPI batch already carries `parent_name=<its
    category>` and `level=2` (forced by `_validate_kpi_batch_items`), so this step:

    1. Round-robin interleaves every category's own KPI batch (unchanged mechanism — if the
       pool ends up over the cap, no single category's whole batch crowds out the others).
    2. Dedupes near-identical KPI CONCEPTS ACROSS THE WHOLE merged pool, not per category in
       isolation — see `_dedupe_kpi_batch_items`'s own docstring for the exact matching rule
       and its "first in round-robin order wins" resolution policy, which is what actually
       decides which category a cross-category duplicate survives under.
    3. Caps the total at MAX_TOTAL_MERGED_KPIS (300: a runaway-protection ceiling, NOT a target).
    4. Groups the survivors by their (surviving) category and renormalizes each category's
       children to sum to 100 WITHIN that category — a relative-emphasis judgment among a
       category's own KPIs, grounded in that category's research. A category that ends up
       with ZERO surviving children (e.g. every one of its proposed KPIs lost a
       cross-category dedup to another category) is dropped entirely — this function never
       emits an empty, childless "category" parent node, since that would itself become a
       leaf with no guidelines and nothing to score.
    5. Categories themselves get NO weight at all (see migration
       0008_category_nodes_no_weight / draft_schema.py's "only leaf KPIs are weighted"
       rule) — purely organizational. To still honor each category's relative IMPORTANCE
       (e.g. Quality should usually outweigh Schedule for a milestone scorecard) without
       storing a weight on the category node itself, each category's `initial_weight`
       guess (see `category_meta`, sourced from `decide_categories`/
       `assess_research_coverage`'s optional `initial_weight` field — an equal default
       share if none was given) is used purely as a SCALING FACTOR: every child's
       in-category-normalized weight is scaled down by that category's renormalized share
       of the whole scorecard, so the LEAVES end up carrying their final GLOBAL weight
       directly. Every leaf across every surviving category is then renormalized together
       ONE MORE time so the full leaf set lands exactly on 100 (the DB's weight-sum rule is
       now global across every leaf in a scorecard version, not per category — see that
       migration), fixing any rounding drift from the two prior normalization passes.

    `category_meta` accumulates across every research round in `research_kpis` (keyed by
    category name — see that function), so a round-2 "deepen an existing category" finding
    (same name as a round-1 category) is grouped and renormalized together with that
    category's round-1 children automatically, with zero special-casing here: grouping by
    name IS the entire "new category vs. deepen an existing one" mechanism.

    **`pinned`** (hybrid user-specified mode — see `_combine_pinned_and_research`): the user's
    own already-complete KPI nodes. They are NEVER passed through dedup, the
    `MAX_TOTAL_MERGED_KPIS` cap or category renormalization (those only ever see the
    RESEARCH proposals, so the cap cannot drop a user KPI and no user KPI can be renamed or
    merged away); research proposals that near-duplicate a pinned name are dropped
    instead, and the two sets are combined at the end. With `pinned=None` (open-ended mode)
    behavior is exactly as described above.

    Returns `[]` (a no-op merge) if no category proposed anything usable — identical
    graceful-degradation contract to this function's pre-category behavior (with `pinned`,
    that case returns just the pinned nodes)."""
    research_part = _merge_research_only(findings, category_meta, pinned, same_pairs)
    if not pinned:
        return research_part
    return _combine_pinned_and_research(pinned, research_part)


def _interleave_findings(findings: list[ResearchFinding]) -> list[dict[str, Any]]:
    """Round-robin interleave of every finding's proposed KPIs (fair first-come ordering for
    the dedup pass — see `_dedupe_kpi_batch_items`)."""
    batches = [f.proposed_kpis for f in findings if f.proposed_kpis]
    interleaved: list[dict[str, Any]] = []
    i = 0
    while any(i < len(batch) for batch in batches):
        for batch in batches:
            if i < len(batch):
                interleaved.append(batch[i])
        i += 1
    return interleaved


def _merge_research_only(
    findings: list[ResearchFinding],
    category_meta: dict[str, dict[str, Any]],
    pinned: list[dict[str, Any]] | None,
    same_pairs: set[frozenset[str]] | None = None,
) -> list[dict[str, Any]]:
    interleaved = _interleave_findings(findings)

    if pinned:
        pinned_norms = [_normalize_kpi_name_for_dedup(str(p["name"])) for p in pinned]
        interleaved = [
            it
            for it in interleaved
            if not _is_near_duplicate(_normalize_kpi_name_for_dedup(str(it.get("name") or "")), pinned_norms)
        ]

    deduped = _dedupe_kpi_batch_items(interleaved, same_pairs)
    capped = deduped[:MAX_TOTAL_MERGED_KPIS]  # safety ceiling only (300) — see MAX_TOTAL_MERGED_KPIS
    if not capped:
        return []

    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in capped:
        by_category.setdefault(str(item.get("parent_name") or ""), []).append(item)
    surviving_names = [name for name in by_category if name]
    if not surviving_names:
        return []

    default_share = 100.0 / len(surviving_names)
    raw_shares: dict[str, float] = {}
    for name in surviving_names:
        initial_weight = (category_meta.get(name) or {}).get("initial_weight")
        raw_shares[name] = float(initial_weight) if initial_weight is not None else default_share
    share_total = sum(raw_shares.values()) or 1.0
    # Each category's renormalized share (0-100) of the WHOLE scorecard — a scaling
    # factor only, never stored as any node's own weight.
    shares = {name: (raw_shares[name] / share_total) * 100.0 for name in surviving_names}

    children: list[dict[str, Any]] = []
    category_nodes: list[dict[str, Any]] = []
    for name in surviving_names:
        category_children = _normalize_weights_to_100(by_category[name])
        scale = shares[name] / 100.0
        for child in category_children:
            if child.get("included_in_scoring", True):
                child = dict(child)
                child["weight"] = round(float(child.get("weight") or 0) * scale, 2)
            children.append(child)
        category_nodes.append(
            {
                "name": name,
                "weight": None,
                "level": 1,
                "parent_name": None,
                "included_in_scoring": True,
                "guidelines": {},
            }
        )

    return [*category_nodes, *_normalize_weights_to_100(children)]


# --- User-specified KPI mode (request classification + pinned KPIs) ---------------------
#
# Problem this section fixes: `research_kpis`'s open-ended fan-out invents categories and KPIs
# and OVERWRITES `draft.kpis`, so a user who says "here is my use case and THESE KPIs — fill
# in the rest" could have their KPIs deduped away, renamed or reweighted, and `propose_kpis`
# would then lock the wrong set in. The fix has three parts:
#
# 1. **Classification** (`_classify_request`): ONE structured-tool Bedrock call, once per
#    session at the top of `research_kpis`, decides the mode from the user's own words —
#    `user_specified` (a use case AND an explicit KPI list), `hybrid` (some KPIs plus "suggest
#    more"), or `open_ended` (no KPI list: the unchanged fan-out). Same `force_tool_use`
#    structured-output pattern as `_decide_categories` (a constrained tool schema with an
#    explicit open_ended "none of the above" label, rather than free-text parsing). ANY failure
#    falls back to open-ended: classification can only ever ADD behaviour, never break a turn.
# 2. **Pinned KPIs**: the user's KPIs (names, hierarchy, any weights/guidelines they gave) are
#    extracted verbatim as a tree and stored in `BuilderState.user_spec["pinned"]` (state
#    only — no draft-schema/DB change). Hard constraints are enforced in code, not just in
#    prompts: the merge never dedupes/caps/renames them (`_merge_research_kpi_batches`), and
#    `update_draft` rejects a patch that drops/renames/re-parents one unless the model
#    declares `user_requested_kpi_changes` (see `_pinned_violations`) — prompt-only
#    constraints are known to weaken as context grows ("constraint weakening"), so the
#    invariant is checked in the harness.
# 3. **Enrichment** (`_enrich_user_kpis`): the model only fills what's MISSING — the 11-level
#    guidelines (optionally grounded by web research scoped to THOSE KPIs) and weights the
#    user didn't give (`_resolve_user_weights`). Its output is matched back to the user's
#    names and everything else is discarded, so it structurally cannot add/remove KPIs.
#
# Weighting policy for missing weights: research-suggested relative importance when
# available, else equal shares — never silently overriding user-given values; weights the
# user gave are only rescaled proportionally when they don't sum to 100 (the framework's
# hard rule), and the user is told. Equal weighting is the defensible default absent
# evidence; the research-suggested weights approximate expert-judgement weighting (as in
# AHP-style scorecards) without asking the user for pairwise comparisons.
#
# Cost/latency: one classify call on every first turn; for user-specified/hybrid, one
# enrichment agent per chunk of MAX_USER_KPIS_PER_ENRICH_CALL KPIs, concurrent (bounded by
# MAX_CONCURRENT_ENRICHMENT_AGENTS), at most MAX_USER_KPI_RESEARCH_AGENTS of which search.
# (the per-call chunk size is `settings.user_kpis_per_fill_call`; research is at most
# MAX_USER_KPI_RESEARCH_AGENTS agents, one search each, no quality gate — see `_research_user_kpis`)
MAX_USER_KPI_RESEARCH_AGENTS = 3
MAX_KPIS_PER_RESEARCH_GROUP = 12
# Hybrid mode: researched additions collectively hold at most this share of the 100-point
# weight budget (user KPIs keep their relative proportions within the remainder).
HYBRID_MAX_RESEARCH_WEIGHT_SHARE = 40.0

USER_SPEC_MODES = ("user_specified", "hybrid", "open_ended")

CLASSIFY_REQUEST_TOOL = ToolSpec(
    name="classify_request",
    description=(
        "Decide how to build this scorecard from the user's own words, and extract any KPIs "
        "they supplied VERBATIM. mode=user_specified: the user gave a use case AND an explicit "
        "list of KPIs (inline in prose, bullets, a table, with or without categories/weights) "
        "and wants the rest filled in. mode=hybrid: they gave some KPIs but also asked for more "
        "to be suggested ('and suggest others', 'add what I'm missing'). mode=open_ended: no "
        "explicit KPI list — they described a goal/domain and want KPIs designed for them. "
        "Topics, themes or examples the user merely mentions in passing are NOT a KPI list."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(USER_SPEC_MODES)},
            "reasoning": {"type": "string", "description": "One short sentence justifying the mode."},
            "kpis": {
                "type": "array",
                "description": (
                    "Every KPI/category the user explicitly named, in the user's order, names "
                    "EXACTLY as written. Empty for open_ended. Never invent, merge, split, "
                    "rename or deduplicate."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "parent_name": {
                            "type": ["string", "null"],
                            "description": "Exact name of this KPI's parent/category in this list; null if top level.",
                        },
                        "weight": {
                            "type": ["number", "null"],
                            "description": "The weight the user gave (as a number), else null.",
                        },
                        "included_in_scoring": {
                            "type": "boolean",
                            "description": "false only if the user said it is tracked but not scored. Default true.",
                        },
                        "user_guidance": {
                            "type": ["string", "null"],
                            "description": (
                                "Any definition, threshold, formula or scoring guidance the user "
                                "gave for this KPI, verbatim; null if none."
                            ),
                        },
                        "guidelines": {
                            "type": ["object", "null"],
                            "description": (
                                "ONLY if the user supplied a full 0-10 rubric for this KPI: "
                                '{"0".."10": {qualitative_text, quantitative_criteria}}; else null.'
                            ),
                        },
                    },
                    "required": ["name"],
                },
            },
            "wants_more_kpis": {
                "type": "boolean",
                "description": "true if the user asked for additional KPIs beyond their own list.",
            },
            "name": {"type": ["string", "null"], "description": "Scorecard name if the user stated one."},
            "purpose": {"type": ["string", "null"], "description": "Purpose if the user stated one."},
            "domain": {"type": ["string", "null"], "description": "Domain if the user stated one."},
            "audience": {"type": ["string", "null"], "description": "Audience if the user stated one."},
            "target_score": {"type": ["number", "null"], "description": "Target score (0-10) if stated."},
            "scoring_formula": {
                "type": ["string", "null"],
                "description": "A custom scoring formula/combination rule if the user gave one, verbatim.",
            },
            "ambiguity_note": {
                "type": ["string", "null"],
                "description": "Only if the mode is genuinely unclear: what is unclear, in one sentence.",
            },
        },
        "required": ["mode", "reasoning", "kpis"],
    },
)

FILL_USER_KPI_DETAILS_TOOL = ToolSpec(
    name="fill_user_kpi_details",
    description=(
        "Write the missing details for EXACTLY the user-defined KPIs listed in the system "
        "prompt, in COMPACT form: per KPI a metric/unit/direction, 11 short level descriptions "
        "(index = score 0..10) and 11 numeric thresholds, plus a suggested relative weight. "
        "Copy each KPI name verbatim. Never add, remove, rename, merge or split KPIs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kpis": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The user's KPI name, verbatim."},
                        "suggested_weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                            "description": (
                                "Relative importance among just the KPIs listed (only used if the user gave none)."
                            ),
                        },
                        "metric": {"type": "string", "description": "What is measured, in <= 8 words."},
                        "unit": {
                            "type": ["string", "null"],
                            "description": 'Unit of the thresholds ("%", "hours", ...) or null.',
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["higher_better", "lower_better", "none"],
                            "description": "none = purely qualitative KPI (then every threshold is null).",
                        },
                        "levels": {
                            "type": "array",
                            "minItems": 11,
                            "maxItems": 11,
                            "items": {"type": "string"},
                            "description": (
                                "EXACTLY 11 strings; index i is the description of score level i (0 = absent/failing, "
                                "10 = best-in-class). Each <= 20 words, concrete, distinct from every other level, "
                                "stating the observable condition (with its number where there is one)."
                            ),
                        },
                        "thresholds": {
                            "type": "array",
                            "minItems": 11,
                            "maxItems": 11,
                            "items": {"type": ["number", "null"]},
                            "description": (
                                "EXACTLY 11 numbers (index = score level) in `unit`: the value needed to reach that "
                                "level, monotonic in the direction of `direction`; nulls when direction is none."
                            ),
                        },
                    },
                    "required": ["name", "suggested_weight", "direction", "levels", "thresholds"],
                },
            },
        },
        "required": ["kpis"],
    },
)

_CLASSIFY_REQUEST_SYSTEM_PROMPT = """You are the request-analysis step for the Quality \
Scorecard System's scorecard-design assistant. Read what the user has said (below) and decide \
whether they have ALREADY defined their own KPIs.

- user_specified: a use case/goal AND an explicit list of KPIs (inline in prose, bullets, a \
table; flat or grouped under categories; weights/formulas/thresholds possibly included). The \
assistant will keep exactly these KPIs and only fill in what's missing.
- hybrid: the user gave some KPIs AND asked for more to be suggested/added around them.
- open_ended: no explicit KPI list — only a goal/domain/problem. The assistant will design the \
KPIs itself. A request like "make me a scorecard for incident response quality", or one that \
only mentions example themes in passing, is open_ended. When in doubt between open_ended and \
the others, choose open_ended unless the user clearly enumerated their own KPIs.

For user_specified/hybrid, extract EVERY KPI the user named, in order, with names EXACTLY as \
written (do not rename, merge, split, deduplicate or add any), their category/parent \
relationships (parent_name = the exact name of the parent in your list), any weight they gave, \
and any definition/threshold/guidance they gave for that KPI. Also capture any scorecard \
name, purpose, domain, audience, target score or custom scoring formula they stated. Leave \
anything the user did NOT state as null.

What the user has said so far (most recent message last):
{conversation_context}

Call `classify_request` exactly once."""

_FILL_INTRO_USER = """The USER has already defined the KPIs below — \
they are AUTHORITATIVE. Your job is only to fill in what is missing for exactly these KPIs: \
a FULL 11-level (0-10) qualitative + quantitative guideline for each, and a suggested \
relative weight."""
_FILL_INTRO_PROPOSED = """The research agents have already chosen the KPIs below \
(name, category and a one-line rationale). Your job is only to write, for exactly these KPIs, \
a FULL 11-level (0-10) qualitative + quantitative guideline for each, and a suggested \
relative weight."""

_FILL_USER_KPIS_SYSTEM_PROMPT_TEMPLATE = """You are the guideline-writing step of the Quality \
Scorecard System's scorecard-design assistant. {intro}

Rules:
- Return exactly the KPIs listed, each `name` copied verbatim. Do NOT add, remove, rename, \
merge or split any KPI, and do not propose extra KPIs.
- Where the user gave guidance for a KPI, honor it exactly (their thresholds/definitions win).
- Replace vague adjectives with real, measurable criteria; where research findings below \
supply benchmarks/thresholds, use them for `thresholds` instead of inventing numbers.
- Level 10 = best-in-class, 0 = absent/failing; levels must be monotonic and distinguishable.
- `levels` holds 11 terse (<= 20 words) descriptions, each stating the observable condition for \
that score and differing from every other level; `thresholds` the 11 matching numbers in `unit` \
(monotonic per `direction`).
- EVERY KPI needs a measurable metric, `unit`, direction ("higher_better" or "lower_better") and 11 \
monotonic numeric thresholds — never "none". For a qualitative KPI use a PROXY measure: the rate of \
reviewed items where the behaviour is present ("% of calls where ..."), a count, a time, or a 0-10 \
reviewer rubric score. Take real benchmark numbers from the research below where they exist; \
otherwise choose sensible targets and do not cite sources you do not have.{proxy_extra}

What the user said (use case/context):
{conversation_context}

The KPIs to detail:
{kpi_lines}

{research_block}Call `fill_user_kpi_details` exactly once."""


@dataclass
class UserSpec:
    """Result of `_classify_request` — what the user explicitly specified. `kpis` is the
    already-normalized KPI tree (see `_normalize_user_kpis`); `notes` records every
    deterministic adjustment made to it (name collisions, depth clamping, ...) so the user
    can be told about them."""

    mode: str
    kpis: list[dict[str, Any]]
    wants_more_kpis: bool = False
    scalars: dict[str, Any] = field(default_factory=dict)
    scoring_formula: str | None = None
    ambiguity_note: str | None = None
    reasoning: str = ""
    notes: list[str] = field(default_factory=list)
    # Mid-conversation lists only: user-named KPIs that already exist in the draft (casefolded
    # name match) — never re-added, but any weight/rubric the user gave for them is applied.
    overlaps: list[dict[str, Any]] = field(default_factory=list)


def _classify_request(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    existing_kpis: list[KpiDraft] | None = None,
) -> UserSpec | None:
    """One synchronous Bedrock call (run via `asyncio.to_thread` by the caller). Returns
    `None` — meaning "treat as open-ended" — for open_ended, a malformed/missing tool
    response, or user_specified/hybrid with no usable KPIs. Never trusts the model's own
    hierarchy levels: those are recomputed from `parent_name` (see `_normalize_user_kpis`).

    `existing_kpis` (mid-conversation use — see `ingest_user_kpis`): the draft's current KPIs.
    The model is told about them (the user's new list may nest under them or repeat them), and
    user-named KPIs that already exist land in `UserSpec.overlaps` instead of `kpis`."""
    system_prompt = _CLASSIFY_REQUEST_SYSTEM_PROMPT.format(conversation_context=conversation_context or "(nothing yet)")
    if existing_kpis:
        system_prompt += (
            "\n\nThe scorecard draft ALREADY contains these KPIs (the user's newest message may nest new KPIs "
            "under them, or repeat some of them): " + "; ".join(f'"{k.name}"' for k in existing_kpis)
            + ". Classify ONLY the KPIs in the user's NEWEST message."
        )
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": "Classify this request."}]}],
        system=system_prompt,
        tools=[CLASSIFY_REQUEST_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_name != "classify_request":
        return None
    data = result.tool_input or {}
    mode = str(data.get("mode") or "")
    if mode not in ("user_specified", "hybrid"):
        return None
    raw_items = data.get("kpis")
    raw_items = raw_items if isinstance(raw_items, list) else []
    existing = {k.name.casefold(): (k.name, k.level) for k in existing_kpis or []}
    overlaps = [
        {**r, "name": existing[" ".join(str(r.get("name") or "").split()).casefold()][0]}
        for r in raw_items
        if isinstance(r, dict) and " ".join(str(r.get("name") or "").split()).casefold() in existing
    ]
    fresh = [
        r
        for r in raw_items
        if not (isinstance(r, dict) and " ".join(str(r.get("name") or "").split()).casefold() in existing)
    ]
    kpis, notes = _normalize_user_kpis(fresh, existing)
    if not kpis and not overlaps:
        return None
    wants_more = bool(data.get("wants_more_kpis"))
    if mode == "user_specified" and wants_more:
        mode = "hybrid"

    scalars: dict[str, Any] = {}
    for key in ("name", "purpose", "domain", "audience"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            scalars[key] = value.strip()
    target = data.get("target_score")
    if isinstance(target, int | float) and not isinstance(target, bool) and 0 <= float(target) <= 10:
        scalars["target_score"] = float(target)
    formula = data.get("scoring_formula")
    return UserSpec(
        mode=mode,
        kpis=kpis,
        wants_more_kpis=wants_more,
        scalars=scalars,
        scoring_formula=formula.strip() if isinstance(formula, str) and formula.strip() else None,
        ambiguity_note=str(data["ambiguity_note"]).strip() if data.get("ambiguity_note") else None,
        reasoning=str(data.get("reasoning") or ""),
        notes=notes,
        overlaps=overlaps,
    )


def _complete_guidelines(guidelines: dict[str, Any]) -> bool:
    return set(guidelines) == {str(i) for i in range(11)}


def _normalize_user_kpis(
    raw_items: list[Any], existing: dict[str, tuple[str, int]] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turns the classifier's raw KPI list into a clean, valid KPI tree WITHOUT ever dropping
    or merging a KPI the user named (no dedup, and no cap — the 30-KPI research ceiling
    does not apply to user KPIs). Deterministic repairs, each reported in the returned
    notes: a duplicate name gets a numeric suffix (names must be unique for `parent_name`
    resolution; `parent_name` references resolve to the FIRST occurrence); an unknown
    parent becomes top level; a parent cycle is broken; hierarchy levels are recomputed from
    the parent chain, and a node deeper than `MAX_HIERARCHY_LEVEL` is re-attached to its
    level-3 ancestor (it stays a leaf at level 4, nothing is lost).

    `existing` (mid-conversation lists — see `ingest_user_kpis`): `{casefolded name: (name,
    level)}` of KPIs already in the draft, which the new list may use as parents; levels are
    then computed relative to that anchor."""
    notes: list[str] = []
    nodes: list[dict[str, Any]] = []
    used: set[str] = set()
    existing = existing or {}
    first_by_key: dict[str, str] = {key: name for key, (name, _level) in existing.items()}
    existing_levels = {name: level for name, level in existing.values()}
    raw_parent: dict[str, str | None] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        original = " ".join(str(raw.get("name") or "").split())
        if not original:
            continue
        name = original
        if name.casefold() in used:
            n = 2
            while f"{original} ({n})".casefold() in used:
                n += 1
            name = f"{original} ({n})"
            notes.append(f'You listed "{original}" more than once; I kept both and named the later one "{name}".')
        used.add(name.casefold())
        first_by_key.setdefault(original.casefold(), name)

        weight: float | None
        try:
            weight = float(raw["weight"]) if raw.get("weight") is not None else None
        except (TypeError, ValueError):
            weight = None
        if weight is not None and not (0 <= weight <= 100):
            weight = None

        guidance = str(raw.get("user_guidance") or "").strip()
        guidelines: dict[str, Any] = {}
        raw_guidelines = raw.get("guidelines")
        if isinstance(raw_guidelines, dict) and raw_guidelines:
            try:
                parsed = KpiDraft.model_validate(
                    _normalize_kpi_guidelines({"name": name, "guidelines": raw_guidelines})
                )
                dumped = {k: v.model_dump(mode="json") for k, v in parsed.guidelines.items()}
                if _complete_guidelines(dumped):
                    guidelines = dumped
                else:  # a partial rubric is guidance for the generator, not a finished rubric
                    guidance = (guidance + " " + json.dumps(dumped)).strip()
            except ValidationError:
                guidance = (guidance + " " + json.dumps(raw_guidelines)).strip()

        parent = raw.get("parent_name")
        raw_parent[name] = " ".join(str(parent).split()) if parent else None
        nodes.append(
            {
                "name": name,
                "parent_name": None,
                "level": 1,
                "weight": weight,
                "included_in_scoring": raw.get("included_in_scoring") is not False,
                "guidelines": guidelines,
                "guidance": guidance,
            }
        )

    parent_of: dict[str, str | None] = {}
    for node in nodes:
        wanted = raw_parent.get(node["name"])
        resolved = first_by_key.get(wanted.casefold()) if wanted else None
        if wanted and (resolved is None or resolved == node["name"]):
            notes.append(
                f'I couldn\'t find the parent "{wanted}" for "{node["name"]}", so I placed it at the top level.'
            )
            resolved = None
        parent_of[node["name"]] = resolved
    for node in nodes:  # break cycles
        seen = {node["name"]}
        cursor = parent_of[node["name"]]
        while cursor is not None:
            if cursor in seen:
                parent_of[node["name"]] = None
                notes.append(f'"{node["name"]}" was part of a circular grouping, so I placed it at the top level.')
                break
            seen.add(cursor)
            cursor = parent_of.get(cursor)
    for node in nodes:
        chain = [node["name"]]
        cursor = parent_of[node["name"]]
        while cursor is not None and cursor in parent_of:  # stop at an existing-draft anchor
            chain.append(cursor)
            cursor = parent_of[cursor]
        anchor = cursor  # None, or the name of an existing draft KPI the chain hangs off
        base = existing_levels.get(anchor, 0) if anchor else 0
        chain.reverse()
        if base + len(chain) > MAX_HIERARCHY_LEVEL:
            target = MAX_HIERARCHY_LEVEL - 1  # re-attach under this node's level-3 ancestor
            if base >= MAX_HIERARCHY_LEVEL:
                node["parent_name"], node["level"] = None, 1
            else:
                node["parent_name"] = anchor if base == target else chain[target - base - 1]
                node["level"] = MAX_HIERARCHY_LEVEL
            notes.append(
                f'"{node["name"]}" was nested deeper than {MAX_HIERARCHY_LEVEL} levels, so I attached it under '
                f'"{node["parent_name"]}" instead.'
                if node["parent_name"]
                else f'"{node["name"]}" was nested too deep, so I placed it at the top level.'
            )
        else:
            node["parent_name"] = parent_of[node["name"]]
            node["level"] = base + len(chain)
    return nodes, notes


def _user_leaf_names(nodes: list[dict[str, Any]]) -> set[str]:
    parents = {n["parent_name"] for n in nodes if n["parent_name"]}
    return {n["name"] for n in nodes if n["name"] not in parents}


def _group_proportions(
    names: list[str], given: dict[str, float | None], suggested: dict[str, float]
) -> tuple[dict[str, float], list[str], float | None]:
    """Turns one sibling group's (possibly partial) user weights into proportions summing to
    1.0. Returns `(proportions, missing_names, given_total_if_all_given)`. Missing weights
    take the remainder when the given ones sum below 100 (the user clearly meant
    percentages), otherwise the mean of the given weights; the remainder is split by the
    research-suggested relative importance when every missing KPI has one, else equally."""
    known: dict[str, float] = {}
    for n in names:
        value = given.get(n)
        if value is not None:
            known[n] = float(value)
    missing = [n for n in names if n not in known]
    raw = dict(known)
    if missing:
        known_sum = sum(known.values())
        if known and known_sum < 100.0:
            budget = 100.0 - known_sum
        elif known:
            budget = (known_sum / len(known)) * len(missing)
        else:
            budget = 100.0
        sugg = {n: float(suggested.get(n) or 0.0) for n in missing}
        if all(v > 0 for v in sugg.values()):
            total_sugg = sum(sugg.values())
            raw.update({n: budget * sugg[n] / total_sugg for n in missing})
        else:
            raw.update({n: budget / len(missing) for n in missing})
    total = sum(raw.values())
    props = {n: raw[n] / total for n in names} if total > 0 else {n: 1.0 / len(names) for n in names}
    return props, missing, (total if not missing else None)


def _resolve_user_weights(nodes: list[dict[str, Any]], suggested: dict[str, float]) -> list[str]:
    """Gives every included LEAF a final weight so the leaves sum to exactly 100 (the
    framework rule — see `ScorecardDraft._sibling_weight_issue`), mutating `nodes` in
    place and returning user-facing notes. User-given weights are kept as given unless they
    don't sum to 100, in which case they are scaled proportionally; missing ones are filled
    (see `_group_proportions`). Two interpretations, chosen by what the user gave: if no
    CATEGORY (non-leaf) carries a weight, leaf weights are global; if categories do carry
    weights, weights are relative within each sibling group and multiplied down the tree.
    Categories never keep a weight (see draft_schema.py)."""
    notes: list[str] = []
    leaves = _user_leaf_names(nodes)
    by_name = {n["name"]: n for n in nodes}
    scored_leaves = [n["name"] for n in nodes if n["name"] in leaves and n["included_in_scoring"]]
    weighted_categories = any(n["weight"] is not None for n in nodes if n["name"] not in leaves)

    final: dict[str, float] = {}
    if scored_leaves and not weighted_categories:
        given = {name: by_name[name]["weight"] for name in scored_leaves}
        props, missing, total_given = _group_proportions(scored_leaves, given, suggested)
        final = {n: props[n] * 100.0 for n in scored_leaves}
        if total_given is not None and abs(total_given - 100.0) > 0.01:
            notes.append(
                f"The weights you gave summed to {total_given:g}, so I scaled them proportionally to total 100."
            )
        if missing:
            how = (
                "using research-informed relative importance"
                if all(suggested.get(m) for m in missing)
                else "as equal shares of the remaining weight"
            )
            notes.append(
                "You didn't give weights for: " + ", ".join(f'"{m}"' for m in missing) + f"; I filled them in {how}."
            )
    elif scored_leaves:
        notes.append(
            "Your category weights were applied as shares of the total, with leaf weights relative within each "
            "category."
        )

        def walk(parent: str | None, share: float) -> None:
            members = [
                n["name"]
                for n in nodes
                if n["parent_name"] == parent and (n["name"] not in leaves or n["included_in_scoring"])
            ]
            if not members:
                return
            props, _missing, _total = _group_proportions(
                members, {m: by_name[m]["weight"] for m in members}, suggested
            )
            for m in members:
                if m in leaves:
                    final[m] = share * props[m]
                else:
                    walk(m, share * props[m])

        walk(None, 100.0)
    if final:
        items = [{"name": n, "weight": w, "included_in_scoring": True} for n, w in final.items()]
        for item in _normalize_weights_to_100(items):
            final[item["name"]] = item["weight"]
    for node in nodes:
        if node["name"] in final:
            node["weight"] = final[node["name"]]
        elif node["name"] not in leaves:
            node["weight"] = None  # categories never carry a weight
    return notes


def _expand_compact_rubric(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Deterministically expands the compact `fill_user_kpi_details` shape (11 short `levels`
    texts + 11 numeric `thresholds` + metric/unit/direction) into the full `{"0".."10":
    {qualitative_text, quantitative_criteria}}` rubric. Returns None (=> the KPI is retried /
    split by the resilient filler) unless there are exactly 11 non-empty, mutually distinct
    level texts. Thresholds that are not numeric or not monotonic in `direction` are dropped
    (the level texts are kept), so a sloppy number never yields an inconsistent rubric."""
    levels = raw.get("levels")
    if not isinstance(levels, list) or len(levels) != 11:
        return None
    texts = [str(t).strip() if isinstance(t, str | int | float) else "" for t in levels]
    if not all(texts) or len({_normalize_kpi_name_for_dedup(t) for t in texts}) != 11:
        return None
    direction = raw.get("direction")
    values: list[float | None] = [None] * 11
    thresholds = raw.get("thresholds")
    if direction in ("higher_better", "lower_better") and isinstance(thresholds, list) and len(thresholds) == 11:
        parsed: list[float | None] = []
        for t in thresholds:
            try:
                parsed.append(None if t is None or isinstance(t, bool) else float(t))
            except (TypeError, ValueError):
                parsed.append(None)
        present = [v for v in parsed if v is not None]
        ordered = present == sorted(present, reverse=direction == "lower_better")
        if len(present) >= 2 and ordered and len(set(present)) >= 2:
            values = parsed
    metric = str(raw.get("metric") or "").strip() or None
    unit = raw.get("unit") if isinstance(raw.get("unit"), str) and raw.get("unit").strip() else None
    out: dict[str, Any] = {}
    for i, text in enumerate(texts):
        value = values[i]
        criteria: dict[str, Any] | None = None
        if value is not None:
            criteria = {
                "metric": metric,
                "operator": ">=" if direction == "higher_better" else "<=",
                "value": int(value) if value == int(value) else value,
            }
            if unit:
                criteria["unit"] = unit
        out[str(i)] = {"qualitative_text": text, "quantitative_criteria": criteria}
    return out


def _validate_filled_user_kpis(raw_items: list[Any], requested: list[str]) -> dict[str, dict[str, Any]]:
    """Matches `fill_user_kpi_details` output back to the user's KPI names (normalized exact
    match only — no fuzzy matching) and keeps only validated guidelines for requested
    names. Anything else the model returned is discarded: this is the structural guarantee
    that enrichment cannot add, remove or rename a user KPI."""
    lookup: dict[str, list[str]] = {}
    for name in requested:
        lookup.setdefault(_normalize_kpi_name_for_dedup(name), []).append(name)
    out: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        key = _normalize_kpi_name_for_dedup(str(raw.get("name") or ""))
        candidates = [c for c in lookup.get(key, []) if c not in out]
        if not candidates:
            continue
        canonical = candidates[0]
        guidelines_raw = _expand_compact_rubric(raw) if "levels" in raw else raw.get("guidelines")
        try:
            kpi = KpiDraft.model_validate(_normalize_kpi_guidelines({"name": canonical, "guidelines": guidelines_raw}))
        except ValidationError:
            logger.warning("fill_user_kpi_details: dropping invalid details for %r", canonical, exc_info=True)
            continue
        if not kpi.guidelines:
            continue
        try:
            weight = float(raw["suggested_weight"]) if raw.get("suggested_weight") is not None else None
        except (TypeError, ValueError):
            weight = None
        out[canonical] = {
            "guidelines": {k: v.model_dump(mode="json") for k, v in kpi.guidelines.items()},
            "suggested_weight": weight if weight is not None and weight > 0 else None,
        }
    return out


def _fill_user_kpi_details(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    chunk: list[dict[str, Any]],
    findings: list[ResearchFinding] | None,
    proposed: bool = False,
    proxy_retry: bool = False,
) -> dict[str, dict[str, Any]]:
    """One synchronous Bedrock call writing guidelines for one chunk of user KPIs."""
    kpi_lines = "\n".join(
        f'- "{n["name"]}"'
        + (f' (under "{n["parent_name"]}")' if n["parent_name"] else "")
        + (f' — {"rationale" if proposed else "user guidance"}: {n["guidance"]}' if n.get("guidance") else "")
        for n in chunk
    )
    research_block = _format_research_findings_for_prompt([asdict(f) for f in findings]) + "\n\n" if findings else ""
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": "Write the details for the user's KPIs now."}]}],
        system=_FILL_USER_KPIS_SYSTEM_PROMPT_TEMPLATE.format(
            conversation_context=conversation_context or "(nothing yet)",
            kpi_lines=kpi_lines,
            research_block=research_block,
            intro=_FILL_INTRO_PROPOSED if proposed else _FILL_INTRO_USER,
            proxy_extra=(
                "\n- RETRY: the previous answer gave these KPIs no usable numeric thresholds. You MUST return "
                "a proxy metric, a unit, a direction and 11 strictly monotonic numbers for EACH."
                if proxy_retry
                else ""
            ),
        ),
        tools=[FILL_USER_KPI_DETAILS_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_name != "fill_user_kpi_details":
        return {}
    raw_items = (result.tool_input or {}).get("kpis")
    return _validate_filled_user_kpis(raw_items if isinstance(raw_items, list) else [], [n["name"] for n in chunk])


PROPOSE_WEIGHTING_TOOL = ToolSpec(
    name="propose_weighting",
    description=(
        "Propose RELATIVE IMPORTANCE for the user's KPIs that have no user-given weight, with a "
        "one-sentence rationale each, and optionally a scoring formula if the use case clearly "
        "calls for one. Copy KPI names verbatim; never add, remove or rename KPIs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "approach": {
                "type": "string",
                "description": "One short sentence naming the weighting logic used (e.g. risk/impact-based).",
            },
            "weights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "relative_importance": {"type": "number", "minimum": 0, "maximum": 100},
                        "rationale": {"type": "string"},
                    },
                    "required": ["name", "relative_importance", "rationale"],
                },
            },
            "scoring_formula": {
                "type": ["string", "null"],
                "description": "Only when allowed and clearly warranted; else null.",
            },
            "formula_rationale": {"type": ["string", "null"]},
        },
        "required": ["approach", "weights"],
    },
)

_PROPOSE_WEIGHTING_SYSTEM_PROMPT_TEMPLATE = """You are the weighting step of the Quality Scorecard \
System's scorecard-design assistant. The USER defined the KPIs below; they are fixed. Propose how \
much each KPI that has no user-given weight should matter relative to the others.

Method: judge importance by RISK/IMPACT for this use case — how costly or harmful poor performance \
on that KPI is, and how directly it reflects the purpose — in the spirit of expert-judgement / \
pairwise (AHP-style) weighting, using the research findings where they speak to it. Do NOT default \
to equal weights unless the KPIs really are interchangeable. Give a rationale of at most 8 words \
each (output tokens are the bottleneck).

{formula_rule}

What the user said (use case/context):
{conversation_context}

KPIs needing a weight (return one entry for EACH, names verbatim):
{missing_lines}

KPIs whose weight the user already fixed (context only — do not return these):
{fixed_lines}

{research_block}Call `propose_weighting` exactly once."""

_FORMULA_ALLOWED_RULE = (
    'OPTIONAL scoring formula: propose `scoring_formula` ONLY if the use case clearly implies a '
    "non-compensatory/gating rule (e.g. a compliance, safety or legal KPI that must not be offset by "
    "strength elsewhere, so a minimum-based or gated combination is more faithful than a weighted "
    'average). Syntax: kpi["Exact KPI Name"], + - * / **, min, max, avg, sqrt, abs. It MUST reference '
    "EVERY scored KPI. Otherwise return null — the default weighted average is usually right."
)
_FORMULA_FORBIDDEN_RULE = "Do NOT propose a scoring formula (return null)."


@dataclass
class WeightingResult:
    """Output of `_run_weighting_step`: `weights` are relative importances (only used for KPIs
    the user didn't weight), `notes` the user-facing rationale, and an optional proposed
    `formula` (validated and applied later by `_apply_user_spec_to_draft`, only when allowed)."""

    weights: dict[str, float]
    notes: list[str]
    formula: str | None = None
    formula_note: str | None = None
    finding: ResearchFinding | None = None


def _scored_leaves_missing_weight(spec: UserSpec) -> list[dict[str, Any]]:
    leaves = _user_leaf_names(spec.kpis)
    if any(n["weight"] is not None for n in spec.kpis if n["name"] not in leaves):
        return []  # category-level weights make the leaf weights group-relative; nothing global to propose
    return [n for n in spec.kpis if n["name"] in leaves and n["included_in_scoring"] and n["weight"] is None]


def _needs_weighting_step(spec: UserSpec) -> bool:
    """Only worth a (research + LLM) step when at least two scored leaves lack a user weight —
    one missing weight is just the remainder, and fully user-weighted lists need nothing."""
    return len(_scored_leaves_missing_weight(spec)) >= 2


def _formula_allowed(spec: UserSpec) -> bool:
    """A model-proposed formula is only ever applied in user_specified mode (in hybrid it would
    silently ignore the researched KPIs), and only if the user supplied neither a formula nor
    any weights (a custom formula would bypass weights they deliberately gave)."""
    return (
        spec.mode == "user_specified"
        and not spec.scoring_formula
        and not any(n["weight"] is not None for n in spec.kpis)
    )


def _propose_weighting(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    spec: UserSpec,
    findings: list[ResearchFinding] | None,
) -> dict[str, Any] | None:
    """One synchronous Bedrock call. Returns validated `{weights, rationales, approach,
    formula, formula_rationale}` restricted to the KPIs that need weights, or None."""
    missing = _scored_leaves_missing_weight(spec)
    fixed = [n for n in spec.kpis if n["weight"] is not None]
    system = _PROPOSE_WEIGHTING_SYSTEM_PROMPT_TEMPLATE.format(
        formula_rule=_FORMULA_ALLOWED_RULE if _formula_allowed(spec) else _FORMULA_FORBIDDEN_RULE,
        conversation_context=conversation_context or "(nothing yet)",
        missing_lines="\n".join(
            f'- "{n["name"]}"' + (f' — {n["guidance"]}' if n.get("guidance") else "") for n in missing
        ),
        fixed_lines="\n".join(f'- "{n["name"]}" = {n["weight"]:g}' for n in fixed) or "(none)",
        research_block=_format_research_findings_for_prompt([asdict(f) for f in findings]) + "\n\n" if findings else "",
    )
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": "Propose the weighting now."}]}],
        system=system,
        tools=[PROPOSE_WEIGHTING_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_name != "propose_weighting":
        return None
    data = result.tool_input or {}
    lookup = {_normalize_kpi_name_for_dedup(n["name"]): n["name"] for n in missing}
    weights: dict[str, float] = {}
    rationales: dict[str, str] = {}
    for raw in data.get("weights") if isinstance(data.get("weights"), list) else []:
        if not isinstance(raw, dict):
            continue
        name = lookup.get(_normalize_kpi_name_for_dedup(str(raw.get("name") or "")))
        try:
            value = float(raw["relative_importance"])
        except (KeyError, TypeError, ValueError):
            continue
        if name is None or name in weights or value <= 0:
            continue
        weights[name] = value
        rationales[name] = str(raw.get("rationale") or "").strip()
    if not weights:
        return None
    formula = data.get("scoring_formula")
    return {
        "weights": weights,
        "rationales": rationales,
        "approach": str(data.get("approach") or "").strip(),
        "formula": formula.strip() if _formula_allowed(spec) and isinstance(formula, str) and formula.strip() else None,
        "formula_rationale": str(data.get("formula_rationale") or "").strip(),
    }


async def _run_weighting_step(
    spec: UserSpec,
    bedrock: BedrockClientProtocol,
    web_search_client: WebSearchClientProtocol | None,
    model_id: str | None,
    *,
    session_id: str,
    turn_started_at: datetime | None,
    conversation_context: str,
    jev_client: JevClientProtocol | None,
    actor: str,
    findings: list[ResearchFinding] | None = None,
) -> WeightingResult | None:
    """The dedicated weighting step for user_specified/hybrid modes (see
    `_needs_weighting_step`): exactly ONE `propose_weighting` call (no extra research of its own —
    it reuses the findings `_research_user_kpis` already gathered). Never raises; None means "no
    proposal" and callers fall back to the per-chunk suggestions / equal shares."""
    if not _needs_weighting_step(spec):
        return None
    missing = _scored_leaves_missing_weight(spec)
    started = time.monotonic()
    try:
        await emit_turn_event(
            session_id, turn_started_at, "master", "weighting",
            f"Working out relative weights for {len(missing)} KPI(s) you didn't weight…",
        )
        # Deliberately NOT behind the shared semaphore: it is one small call, and queueing it
        # behind the guideline fan-out made it wait a whole wave (127s observed).
        raw = await asyncio.to_thread(_propose_weighting, bedrock, model_id, conversation_context, spec, findings)
        if raw is None:
            return WeightingResult(weights={}, notes=[])
        listed = "; ".join(f'{n} — {raw["rationales"].get(n) or "no rationale given"}' for n in raw["weights"])
        note = (
            f"I proposed relative weights for {', '.join(raw['weights'])} using risk/impact-based importance"
            + (f" ({raw['approach']})" if raw["approach"] else "")
            + f": {listed}."
        )
        await emit_turn_event(
            session_id, turn_started_at, "master", "weighting", f"Proposed weights for {len(raw['weights'])} KPI(s)."
        )
        await _emit_phase(session_id, turn_started_at, "weighting", started)
        return WeightingResult(
            weights=raw["weights"], notes=[note], formula=raw["formula"],
            formula_note=raw["formula_rationale"] or None,
        )
    except Exception:  # noqa: BLE001 — a failed weighting step must never break the turn
        logger.warning("_run_weighting_step failed; falling back to default weight fill.", exc_info=True)
        return None


async def _emit_phase(session_id: str, turn_started_at: datetime | None, phase: str, started: float) -> None:
    """Master 'timing' trace event with the wall-clock seconds one phase took (classify, enrich,
    weighting, reconcile) so a trace shows where a turn's time went. Also logged."""
    elapsed = time.monotonic() - started
    logger.info("phase timing: %s took %.1fs", phase, elapsed)
    await emit_turn_event(session_id, turn_started_at, "master", "timing", f"Phase '{phase}' took {elapsed:.1f}s.")


@dataclass
class PinnedKpis:
    """Output of `_prepare_pinned_kpis`: the user's KPI nodes, complete and ready to drop into
    `draft.kpis` (guidelines filled where possible, leaf weights summing to 100), plus the
    research findings gathered for them, the notes to tell the user, and an optional
    weighting-step scoring formula proposal (see `_apply_user_spec_to_draft`)."""

    kpis: list[dict[str, Any]]
    findings: list[ResearchFinding]
    notes: list[str]
    formula: str | None = None
    formula_note: str | None = None


_FALLBACK_BANDS = (
    "absent or failing", "far below expectations", "well below expectations", "below expectations",
    "marginal", "partially meets expectations", "meets the basic expectation", "good",
    "very good", "excellent", "best-in-class",
)


def _fallback_guidelines(name: str) -> dict[str, dict[str, Any]]:
    """LAST-RESORT rubric for a KPI whose guideline calls all failed: clearly marked as an
    automatic fallback so the user (and the master step) know to refine it. Complete and
    monotonic (levels 0-10), but deliberately generic — it carries no invented numbers."""
    return {
        str(level): {
            "qualitative_text": (
                f"[Auto-generated fallback rubric — please refine] Level {level}/10: performance on "
                f'"{name}" is {_FALLBACK_BANDS[level]}.'
            ),
            "quantitative_criteria": None,
        }
        for level in range(11)
    }


def _incomplete(got: dict[str, dict[str, Any]], chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n for n in chunk if n["name"] not in got or not _complete_guidelines(got[n["name"]]["guidelines"])]


async def _fill_attempt(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    chunk: list[dict[str, Any]],
    findings: list[ResearchFinding] | None,
    proposed: bool = False,
    proxy_retry: bool = False,
) -> dict[str, dict[str, Any]]:
    """One guideline-writing call under the shared Bedrock concurrency cap. A timeout or any
    other Bedrock failure yields `{}` (the caller then retries the missing KPIs smaller)."""
    try:
        async with _bedrock_slots():
            return await asyncio.to_thread(
                _fill_user_kpi_details, bedrock, model_id, conversation_context, chunk, findings, proposed, proxy_retry
            )
    except BedrockTimeoutError:
        logger.warning("guideline fill timed out for %d KPI(s): %s", len(chunk), [n["name"] for n in chunk])
    except BedrockUnavailableError:
        logger.warning("guideline fill failed (Bedrock) for %d KPI(s).", len(chunk), exc_info=True)
    except Exception:  # noqa: BLE001 — malformed output etc.: treated like a missing result
        logger.warning("guideline fill failed for %d KPI(s).", len(chunk), exc_info=True)
    return {}


async def _fill_chunk_resilient(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    chunk: list[dict[str, Any]],
    findings: list[ResearchFinding] | None,
    *,
    deadline: float,
    proposed: bool = False,
    proxy_retry: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Writes guidelines for `chunk` and NEVER leaves a KPI without a rubric: full chunk ->
    (for whatever is missing/incomplete, e.g. after a read timeout) ONE retry at HALF the size,
    run concurrently -> single KPIs -> a clearly marked generic fallback rubric. Past `deadline`
    (a `time.monotonic()` value) the research context is dropped and the remaining KPIs go
    straight to the smallest calls, to bound the phase. Returns `(details, fallback_names)`."""
    got = await _fill_attempt(bedrock, model_id, conversation_context, chunk, findings, proposed, proxy_retry)
    for _round in range(2):  # halves, then singles
        missing = _incomplete(got, chunk)
        if not missing:
            break
        size = 1 if _round == 1 or time.monotonic() > deadline else max(1, -(-len(missing) // 2))
        pieces = [missing[i : i + size] for i in range(0, len(missing), size)]
        ctx_findings = None if time.monotonic() > deadline else findings
        results = await asyncio.gather(
            *(
                _fill_attempt(bedrock, model_id, conversation_context, p, ctx_findings, proposed, proxy_retry)
                for p in pieces
            )
        )
        for part in results:
            for name, value in part.items():
                if name not in got or _complete_guidelines(value["guidelines"]):
                    got[name] = value
    fallback: list[str] = []
    for n in _incomplete(got, chunk):
        got[n["name"]] = {
            "guidelines": _fallback_guidelines(n["name"]),
            "suggested_weight": (got.get(n["name"]) or {}).get("suggested_weight"),
        }
        fallback.append(n["name"])
    return got, fallback


MIN_CRITERIA_LEVELS = 6  # levels (of 11) that must carry numeric quantitative_criteria for a rubric to count
PROPOSED_TARGET_MARKER = " [proposed target — please validate]"


def _criteria_levels(guidelines: dict[str, Any]) -> int:
    return sum(1 for g in guidelines.values() if (g or {}).get("quantitative_criteria"))


async def _fill_missing_guidelines(
    items: list[dict[str, Any]],
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    findings: list[ResearchFinding] | None,
    *,
    deadline: float | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Writes COMPLETE 11-level rubrics for KPIs that lack them (open-ended pipeline + the safety net
    that repairs any leaf found incomplete). `items` are `{name, parent_name, guidance}` dicts; they
    are split into compact-rubric chunks of `settings.user_kpis_per_fill_call` KPIs run concurrently
    (bounded by the shared Bedrock slots), each via `_fill_chunk_resilient` (retry in halves, then
    singles, then the marked fallback rubric). Returns `({name: guidelines}, fallback_names)`;
    every requested name is present in the result. Never raises."""
    if not items:
        return {}, []
    settings = get_settings()
    if deadline is None:
        deadline = time.monotonic() + settings.open_fill_deadline_seconds
    per_call = max(1, settings.user_kpis_per_fill_call)
    chunks = [items[i : i + per_call] for i in range(0, len(items), per_call)]
    out: dict[str, dict[str, Any]] = {}
    fallback: list[str] = []

    async def run(chunk: list[dict[str, Any]]) -> None:
        try:
            got, fb = await _fill_chunk_resilient(
                bedrock, model_id, conversation_context, chunk, findings, deadline=deadline, proposed=True
            )
        except Exception:  # noqa: BLE001 — never leave a KPI without a rubric
            logger.warning("guideline fill chunk failed; using fallback rubrics.", exc_info=True)
            got, fb = {}, []
        for n in chunk:
            value = got.get(n["name"])
            if value is None or not _complete_guidelines(value["guidelines"]):
                value = {"guidelines": _fallback_guidelines(n["name"])}
                if n["name"] not in fb:
                    fb = [*fb, n["name"]]
            out[n["name"]] = value["guidelines"]
        fallback.extend(fb)

    await asyncio.gather(*(run(c) for c in chunks))

    # Measurable-proxy check: a rubric where fewer than MIN_CRITERIA_LEVELS levels carry numeric criteria
    # (the model used direction "none" / sloppy thresholds) is re-requested ONCE with an explicit demand
    # for a proxy metric; whatever still lacks numbers keeps its qualitative text, honestly marked.
    lacking = [
        n for n in items
        if n["name"] not in fallback and _criteria_levels(out[n["name"]]) < MIN_CRITERIA_LEVELS
    ]
    if lacking and time.monotonic() < deadline:
        async def retry(chunk: list[dict[str, Any]]) -> None:
            try:
                got, _fb = await _fill_chunk_resilient(
                    bedrock, model_id, conversation_context, chunk, findings,
                    deadline=deadline, proposed=True, proxy_retry=True,
                )
            except Exception:  # noqa: BLE001 — keep the first answer
                logger.warning("proxy-metric retry failed.", exc_info=True)
                return
            for n in chunk:
                value = got.get(n["name"])
                if (
                    value is not None
                    and _complete_guidelines(value["guidelines"])
                    and _criteria_levels(value["guidelines"]) > _criteria_levels(out[n["name"]])
                    and "Auto-generated fallback" not in value["guidelines"]["0"]["qualitative_text"]
                ):
                    out[n["name"]] = value["guidelines"]

        await asyncio.gather(*(retry(lacking[i : i + per_call]) for i in range(0, len(lacking), per_call)))
    no_numeric = 0
    for n in items:
        if n["name"] in fallback or _criteria_levels(out[n["name"]]) >= MIN_CRITERIA_LEVELS:
            continue
        no_numeric += 1
        top = out[n["name"]]["10"]
        if PROPOSED_TARGET_MARKER not in top["qualitative_text"]:
            out[n["name"]] = {
                **out[n["name"]], "10": {**top, "qualitative_text": top["qualitative_text"] + PROPOSED_TARGET_MARKER}
            }
    if stats is not None:
        stats["no_numeric"] = stats.get("no_numeric", 0) + no_numeric
        stats["total"] = stats.get("total", 0) + len(items)
    return out, fallback


def _incomplete_leaves(kpis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LEAF KPI dicts (never a category) whose guidelines are not all 11 levels."""
    parents = {k.get("parent_name") for k in kpis if k.get("parent_name")}
    return [k for k in kpis if k["name"] not in parents and not _complete_guidelines(k.get("guidelines") or {})]


async def _repair_incomplete_leaves(
    kpis: list[dict[str, Any]],
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    *,
    session_id: str | None = None,
    turn_started_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Safety net: every leaf in `kpis` ends with all 11 guideline levels. Incomplete leaves are
    re-requested in small compact chunks; only if that fails do they get the marked fallback rubric.
    Returns `(kpis, fallback_names)`; the same list when nothing was incomplete."""
    todo = _incomplete_leaves(kpis)
    if not todo:
        return kpis, []
    items = [{"name": k["name"], "parent_name": k.get("parent_name"), "guidance": ""} for k in todo]
    if session_id is not None:
        await emit_turn_event(
            session_id, turn_started_at, "master", "guidelines",
            f"Repairing incomplete guidelines for {len(items)} KPI(s)…",
        )
    filled, fallback = await _fill_missing_guidelines(items, bedrock, model_id, conversation_context, None)
    repaired = [{**k, "guidelines": filled[k["name"]]} if k["name"] in filled else k for k in kpis]
    return repaired, fallback


def _research_groups(names: list[str]) -> list[list[str]]:
    """Splits KPI names into at most MAX_USER_KPI_RESEARCH_AGENTS contiguous groups."""
    if not names:
        return []
    n_groups = min(MAX_USER_KPI_RESEARCH_AGENTS, -(-len(names) // MAX_KPIS_PER_RESEARCH_GROUP))
    size = -(-len(names) // n_groups)
    return [names[i : i + size] for i in range(0, len(names), size)]


async def _research_user_kpis(
    spec: UserSpec,
    bedrock: BedrockClientProtocol,
    web_search_client: WebSearchClientProtocol | None,
    model_id: str | None,
    *,
    session_id: str,
    turn_started_at: datetime | None,
    conversation_context: str,
    jev_client: JevClientProtocol | None,
    actor_offset: int = 0,
) -> list[ResearchFinding]:
    """Bounded research over the user's KPIs: at most MAX_USER_KPI_RESEARCH_AGENTS agents (each
    grouping ~12 KPIs), ONE search each, NO quality gate, all concurrent under one wall-clock
    budget (`settings.user_research_timeout_seconds`); an agent that doesn't finish in time is
    dropped and its KPIs are written from model knowledge. Never raises."""
    if web_search_client is None or not _web_search_usable(web_search_client):
        return []
    leaves = _user_leaf_names(spec.kpis)
    names = [n["name"] for n in spec.kpis if n["name"] in leaves and n["included_in_scoring"]]
    timeout = get_settings().user_research_timeout_seconds

    async def one(index: int, group: list[str]) -> ResearchFinding | None:
        joined = ", ".join(group)
        actor = f"research_agent_{actor_offset + index + 1}"
        try:
            found = await asyncio.wait_for(
                _run_research_agent(
                    f"Benchmarks for your KPIs: {joined}"[:200],
                    "Find real, citable benchmarks, thresholds and industry-typical relative importance for "
                    "EXACTLY these user-defined KPIs (do not suggest other KPIs). Use ONE search covering the "
                    f"most important ones: {joined}.",
                    bedrock, web_search_client, model_id,
                    session_id=session_id, turn_started_at=turn_started_at, actor=actor,
                    jev_client=jev_client, conversation_context=conversation_context,
                    propose_batch=False, max_searches=1, use_quality_gate=False,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            await emit_turn_event(
                session_id, turn_started_at, actor, "error",
                f"Research took longer than {timeout}s — writing these guidelines from model knowledge instead.",
            )
            return None
        if found.has_content() and not found.degraded:
            found.suggested_kpis = []  # the KPI set is the user's — research must not suggest others
            return found
        return None

    results = await asyncio.gather(*(one(i, g) for i, g in enumerate(_research_groups(names))))
    return [f for f in results if f is not None]


async def _enrich_user_kpis(
    spec: UserSpec,
    bedrock: BedrockClientProtocol,
    web_search_client: WebSearchClientProtocol | None,
    model_id: str | None,
    *,
    session_id: str,
    turn_started_at: datetime | None,
    conversation_context: str,
    jev_client: JevClientProtocol | None,
    actor_offset: int = 0,
    findings: list[ResearchFinding] | None = None,
    deadline: float | None = None,
    on_progress: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[ResearchFinding], list[str]]:
    """Fills guidelines (+ suggested weights) for every user LEAF KPI that lacks a complete
    user-supplied rubric. Small chunks (`settings.user_kpis_per_fill_call` KPIs) run
    concurrently, bounded by the shared Bedrock slot cap, each via `_fill_chunk_resilient`
    (timeout -> half-size retry -> singles -> marked fallback), so no leaf is left without
    guidelines. `findings` are the research findings from `_research_user_kpis` (when the caller
    already ran research they are reused; otherwise it runs here, bounded). `deadline`
    (`time.monotonic()`) bounds the phase: once passed, remaining work drops research context
    and uses the smallest calls. Returns `(details, findings, fallback_names)` — the last lists KPIs
    that got the generic marked fallback rubric. Never raises."""
    settings = get_settings()
    if deadline is None:
        deadline = time.monotonic() + settings.user_enrich_deadline_seconds
    if findings is None:
        findings = await _research_user_kpis(
            spec, bedrock, web_search_client, model_id,
            session_id=session_id, turn_started_at=turn_started_at,
            conversation_context=conversation_context, jev_client=jev_client, actor_offset=actor_offset,
        )
    leaves = _user_leaf_names(spec.kpis)
    todo = [n for n in spec.kpis if n["name"] in leaves and not n["guidelines"]]
    per_call = max(1, settings.user_kpis_per_fill_call)
    chunks = [todo[i : i + per_call] for i in range(0, len(todo), per_call)]
    details: dict[str, dict[str, Any]] = {}
    fallback_names: list[str] = []
    done = 0

    async def run_chunk(chunk: list[dict[str, Any]]) -> None:
        nonlocal done
        try:
            got, fb = await _fill_chunk_resilient(
                bedrock, model_id, conversation_context, chunk, findings or None, deadline=deadline
            )
            details.update(got)
            fallback_names.extend(fb)
            if on_progress is not None:
                try:
                    on_progress(dict(details))  # progressive fill: the live preview shows finished KPIs
                except Exception:  # noqa: BLE001 — a preview hiccup must never fail the fill
                    logger.warning("_enrich_user_kpis: progress callback failed.", exc_info=True)
        except Exception:  # noqa: BLE001 — a failed chunk must never break the turn (unfilled -> propose_kpis)
            logger.warning("_enrich_user_kpis: chunk failed.", exc_info=True)
        done += len(chunk)
        await emit_turn_event(
            session_id, turn_started_at, "master", "guidelines",
            f"Wrote guidelines for {min(done, len(todo))}/{len(todo)} of your KPIs…",
        )

    if todo:
        await emit_turn_event(
            session_id, turn_started_at, "master", "guidelines",
            f"Writing 0-10 guidelines for {len(todo)} KPI(s) in {len(chunks)} parallel call(s)…",
        )
    await asyncio.gather(*(run_chunk(c) for c in chunks), return_exceptions=True)
    return details, findings, fallback_names


async def _prepare_pinned_kpis(
    spec: UserSpec,
    bedrock: BedrockClientProtocol,
    web_search_client: WebSearchClientProtocol | None,
    model_id: str | None,
    *,
    session_id: str,
    turn_started_at: datetime | None,
    conversation_context: str,
    jev_client: JevClientProtocol | None,
    actor_offset: int = 0,
    standalone_weights: bool = True,
    on_progress: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> PinnedKpis:
    """Enrich + weight + assemble the user's KPI nodes (see the section comment above). The
    user's names/hierarchy/included flags are copied from `spec.kpis` untouched; only
    `guidelines` and missing `weight`s are filled. Guideline enrichment and the dedicated
    weighting step (`_run_weighting_step`) run concurrently. Falls back to the un-enriched
    nodes (no guidelines, equal-share weights) on an unexpected failure — pinned KPIs are
    never lost.

    `standalone_weights=False` (mid-conversation lists — `ingest_user_kpis`) skips the
    "leaves sum to 100 among themselves" resolution: the caller rebalances against the
    draft's existing KPIs instead; missing weights are left as the (relative) weighting-step
    suggestion or None."""
    details: dict[str, dict[str, Any]] = {}
    findings: list[ResearchFinding] = []
    fallback_names: list[str] = []
    weighting: WeightingResult | None = None
    enrichment_slots = _count_enrichment_chunks(spec)
    started = time.monotonic()
    # 1. bounded research (<= 3 agents, one search each, no gate, ~45s cap) ...
    try:
        findings = await _research_user_kpis(
            spec, bedrock, web_search_client, model_id,
            session_id=session_id, turn_started_at=turn_started_at,
            conversation_context=conversation_context, jev_client=jev_client, actor_offset=actor_offset,
        )
    except Exception:  # noqa: BLE001 — research is optional grounding
        logger.warning("_prepare_pinned_kpis: research failed; continuing from model knowledge.", exc_info=True)
    # 2. ... then guideline writing (priority) and the single weighting call concurrently.
    deadline = started + get_settings().user_enrich_deadline_seconds
    # The weighting call is ONE small call: it goes FIRST so it takes a Bedrock slot before the
    # (many) guideline calls queue behind the semaphore — it used to wait a whole wave (127s).
    results = await asyncio.gather(
        _run_weighting_step(
            spec, bedrock, web_search_client, model_id,
            session_id=session_id, turn_started_at=turn_started_at,
            conversation_context=conversation_context, jev_client=jev_client,
            actor=f"research_agent_{actor_offset + enrichment_slots + 1}", findings=findings,
        ),
        _enrich_user_kpis(
            spec, bedrock, web_search_client, model_id,
            session_id=session_id, turn_started_at=turn_started_at,
            conversation_context=conversation_context, jev_client=jev_client, actor_offset=actor_offset,
            findings=findings, deadline=deadline, on_progress=on_progress,
        ),
        return_exceptions=True,
    )
    results = [results[1], results[0]]
    if isinstance(results[0], BaseException):
        logger.warning("_prepare_pinned_kpis: enrichment failed; using un-enriched KPIs.", exc_info=results[0])
    else:
        details, findings, fallback_names = results[0]
    if isinstance(results[1], BaseException):
        logger.warning("_prepare_pinned_kpis: weighting failed.", exc_info=results[1])
    else:
        weighting = results[1]
    await _emit_phase(session_id, turn_started_at, "enrich", started)

    nodes = [dict(n) for n in spec.kpis]
    suggested = {name: d["suggested_weight"] for name, d in details.items() if d.get("suggested_weight")}
    if weighting is not None:
        suggested.update(weighting.weights)  # the dedicated step supersedes per-chunk guesses
    notes = list(spec.notes)
    if standalone_weights:
        notes += _resolve_user_weights(nodes, suggested)
    else:
        for n in nodes:
            if n["weight"] is None and n["name"] in suggested and n["included_in_scoring"]:
                n["weight"] = suggested[n["name"]]
    if weighting is not None and weighting.weights:
        notes += weighting.notes
    leaves = _user_leaf_names(nodes)
    kpis: list[dict[str, Any]] = []
    unfilled: list[str] = []
    for node in nodes:
        is_leaf = node["name"] in leaves
        guidelines = node["guidelines"] or (details.get(node["name"], {}).get("guidelines") if is_leaf else None)
        if is_leaf and not guidelines:
            unfilled.append(node["name"])
        kpis.append(
            KpiDraft(
                name=node["name"],
                weight=node["weight"],
                level=node["level"],
                parent_name=node["parent_name"],
                included_in_scoring=node["included_in_scoring"],
                guidelines=guidelines or {},
            ).model_dump(mode="json")
        )
    if fallback_names:
        notes.append(
            "The model couldn't produce guidelines for these in time, so I gave them a generic placeholder rubric "
            "(marked '[Auto-generated fallback rubric]') that you should refine: "
            + ", ".join(f'"{u}"' for u in fallback_names) + "."
        )
    if unfilled:
        notes.append("I couldn't generate guidelines yet for: " + ", ".join(f'"{u}"' for u in unfilled) + ".")
    return PinnedKpis(
        kpis=kpis,
        findings=findings,
        notes=notes,
        formula=weighting.formula if weighting is not None else None,
        formula_note=weighting.formula_note if weighting is not None else None,
    )


def _combine_pinned_and_research(pinned: list[dict[str, Any]], research: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hybrid merge: the user's nodes are kept exactly (names, hierarchy, guidelines);
    researched additions are added around them. A research CATEGORY named like a pinned
    category reuses it (the new KPIs nest under the user's category); named like a pinned
    leaf, it is renamed "<name> (suggested)" so no user KPI is ever shadowed. Weights: the
    researched leaves collectively take `min(HYBRID_MAX_RESEARCH_WEIGHT_SHARE, their
    proportional share)` of the 100-point budget and the user's leaves keep their relative
    proportions within the remainder (unavoidable once KPIs are added: the leaf weights
    must still sum to 100). Levels are recomputed from the final tree."""
    pinned_nodes = [dict(p) for p in pinned]
    if not research:
        return pinned_nodes
    pinned_parents = {p["parent_name"] for p in pinned_nodes if p["parent_name"]}
    pinned_by_key = {p["name"].casefold(): p["name"] for p in pinned_nodes}
    rename: dict[str, str] = {}
    research_nodes: list[dict[str, Any]] = []
    for raw_node in research:
        node = dict(raw_node)
        if node.get("level") == 1 and node.get("parent_name") is None:
            existing = pinned_by_key.get(node["name"].casefold())
            if existing is not None and existing in pinned_parents:
                rename[node["name"]] = existing
                continue  # reuse the user's category node; its research children nest under it
            if existing is not None:
                rename[node["name"]] = f'{node["name"]} (suggested)'
                node["name"] = rename[node["name"]]
        research_nodes.append(node)
    for node in research_nodes:
        if node.get("parent_name") in rename:
            node["parent_name"] = rename[node["parent_name"]]

    def scored_leaves(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parents = {n["parent_name"] for n in nodes if n.get("parent_name")}
        return [
            n
            for n in nodes
            if n["name"] not in parents and n.get("included_in_scoring", True) and n.get("weight") is not None
        ]

    user_leaves = scored_leaves(pinned_nodes)
    # A research node that is a leaf of the COMBINED tree (it may now parent nothing).
    new_leaves = scored_leaves(research_nodes)
    if user_leaves and new_leaves:
        research_share = min(
            HYBRID_MAX_RESEARCH_WEIGHT_SHARE, 100.0 * len(new_leaves) / (len(new_leaves) + len(user_leaves))
        )
    else:
        research_share = 100.0 if new_leaves else 0.0
    for leaf in user_leaves:
        leaf["weight"] = float(leaf["weight"]) * (100.0 - research_share) / 100.0
    for leaf in new_leaves:
        leaf["weight"] = float(leaf["weight"]) * research_share / 100.0
    all_leaves = [*user_leaves, *new_leaves]
    for original, normalized in zip(all_leaves, _normalize_weights_to_100([dict(n) for n in all_leaves]), strict=True):
        original["weight"] = normalized["weight"]

    combined = [*pinned_nodes, *research_nodes]
    by_name = {n["name"]: n for n in combined}
    for node in combined:
        depth, cursor = 1, node.get("parent_name")
        while cursor is not None and depth <= MAX_HIERARCHY_LEVEL:
            depth += 1
            cursor = (by_name.get(cursor) or {}).get("parent_name")
        node["level"] = min(depth, MAX_HIERARCHY_LEVEL)
    return combined


def _apply_user_spec_to_draft(
    draft_dict: dict[str, Any], spec: UserSpec, notes: list[str], pinned: PinnedKpis | None = None
) -> dict[str, Any]:
    """Applies the scalar fields (name/purpose/domain/audience/target_score) and the
    scoring formula to a draft dict that already holds the final KPI set. The user's own
    formula wins; otherwise a formula proposed by the weighting step (`pinned.formula`) is
    applied only if `_formula_allowed` and it validates AND references every scored KPI (a
    gating formula that silently ignores a KPI would drop it from the score). Anything
    invalid is skipped (a user-formula error is reported in `notes` so the model can fix it
    via `update_scoring_formula`) — never raises."""
    out = {**draft_dict, **spec.scalars}
    names = [k["name"] for k in out.get("kpis") or []]
    if spec.scoring_formula:
        check = validate_scoring_formula(spec.scoring_formula, names)
        if check.valid:
            out["scoring_formula"] = spec.scoring_formula
        else:
            notes.append(
                f"I couldn't apply your scoring formula {spec.scoring_formula!r} as written ({check.error}); "
                "it needs a fix before it can be set."
            )
    elif pinned is not None and pinned.formula and _formula_allowed(spec):
        check = validate_scoring_formula(pinned.formula, names)
        if check.valid and not check.unused_kpis:
            out["scoring_formula"] = pinned.formula
            notes.append(
                f"I set a custom scoring formula {pinned.formula!r}"
                + (f" because {pinned.formula_note}" if pinned.formula_note else " because your use case suggests it")
                + " — say so if you'd rather use the default weighted average."
            )
        else:
            logger.info("proposed scoring formula %r rejected by validation (%s).", pinned.formula, check.error)
    try:
        ScorecardDraft.model_validate(out)
    except ValidationError:
        logger.warning("_apply_user_spec_to_draft: spec fields failed validation; skipping them.", exc_info=True)
        return dict(draft_dict)
    return out


def _guidelines_hash(guidelines: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(guidelines, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _user_supplied_names(nodes: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Which KPIs the USER (not the model) supplied a weight / complete rubric for. If a
    category carries a user weight, every scored leaf's weight derives from user input."""
    leaves = _user_leaf_names(nodes)
    category_weighted = any(n["weight"] is not None for n in nodes if n["name"] not in leaves)
    weights = {
        n["name"]
        for n in nodes
        if n["name"] in leaves and n["included_in_scoring"] and (n["weight"] is not None or category_weighted)
    }
    return weights, {n["name"] for n in nodes if n["guidelines"]}


def _pin_entries(
    nodes: list[dict[str, Any]], final_kpis: list[dict[str, Any]], user_weights: set[str], user_guidelines: set[str]
) -> list[dict[str, Any]]:
    """Pinned-KPI records: hierarchy always; `weight`/`guidelines_hash` only for values the
    user themselves supplied (frozen at their FINAL values — after normalization/rebalancing),
    so model-filled weights/guidelines stay freely editable (see `_pinned_violations`)."""
    by_name = {k["name"]: k for k in final_kpis}
    entries: list[dict[str, Any]] = []
    for n in nodes:
        k = by_name.get(n["name"], n)
        entries.append(
            {
                "name": n["name"],
                "parent_name": k.get("parent_name"),
                "level": k.get("level", n["level"]),
                "weight": k.get("weight") if n["name"] in user_weights else None,
                "guidelines_hash": (
                    _guidelines_hash(k.get("guidelines") or {}) if n["name"] in user_guidelines else None
                ),
            }
        )
    return entries


def _user_spec_state(spec: UserSpec, final_kpis: list[dict[str, Any]], notes: list[str]) -> dict[str, Any]:
    """The compact, checkpointed `BuilderState.user_spec` (see `_pinned_violations`)."""
    user_weights, user_guidelines = _user_supplied_names(spec.kpis)
    return {
        "mode": spec.mode,
        "pinned": _pin_entries(spec.kpis, final_kpis, user_weights, user_guidelines),
        "notes": notes,
        "allow_additional": spec.wants_more_kpis or spec.mode == "hybrid",
        "ambiguity_note": spec.ambiguity_note,
        "scoring_formula_hint": spec.scoring_formula,
    }


def _format_user_spec_for_prompt(user_spec: dict[str, Any] | None) -> str:
    """The USER-SPECIFIED KPIs block woven into propose_kpis's system prompt (empty when no
    KPIs are pinned). The hard rule here is also enforced in code by `update_draft`."""
    spec = user_spec or {}
    pinned = spec.get("pinned") or []
    if not pinned:
        return ""
    more = bool(spec.get("allow_additional"))
    lines = [
        "USER-SPECIFIED KPIs — AUTHORITATIVE (the user defined these themselves"
        + (", and also asked you to suggest more around them" if more else "")
        + "):",
        "Pinned KPIs: "
        + "; ".join(
            f'"{p["name"]}"' + (f' (under "{p["parent_name"]}")' if p.get("parent_name") else "") for p in pinned
        ),
        "RULES: never remove, rename, merge or re-parent these, and never change a weight or guideline the USER "
        "supplied (the server rejects it; weights/guidelines YOU filled in are freely editable — when KPIs are "
        "added or removed, rebalance leaf weights to total 100 by changing only those, and tell the user). If you "
        "send `kpis` in update_draft it MUST include every pinned KPI with its exact name and parent_name. You are "
        "only filling gaps (scalar fields, missing guidelines, an optional scoring formula) and explaining "
        "assumptions, including the weighting rationale in the notes below. "
        + (
            "Add further KPIs only as the user asked, around their own. "
            if more
            else "Do NOT add extra KPIs unless the user asks. "
        )
        + "If the user explicitly asks (naming the KPI) to remove, rename, reweight or rewrite a pinned KPI, do it "
        "and list its exact ORIGINAL name in update_draft's `user_requested_kpi_changes` — the server checks the "
        "user's latest message; if they were vague (e.g. \"drop the second one\"), confirm first via "
        "ask_clarification naming the KPI. "
        "COMPLETE THE DRAFT YOURSELF: the user's use case is in their message, so infer the scorecard name, "
        "purpose, domain, audience and target score (0-10) from it and set them with ONE update_draft (patch "
        "with just those fields; omit `kpis`) — NEVER ask the user for any of them. Present the COMPLETE draft "
        "(KPIs, weights, guidelines are already filled) in `assistant_message`, then, if you need anything, ask "
        "only the single question \"save it as-is or adjust something?\" via ask_clarification. Do NOT call "
        "web_search for the user's own KPIs — research has already been done. "
        "ALSO: if the user's message contains a direct question alongside their KPIs (e.g. \"why is this "
        "scorecard needed?\", \"what is it for?\"), ANSWER it in the first sentences of your message to the user "
        "(`assistant_message`, or `respond_conversationally`'s reply) — grounded in the use case and KPIs they "
        "gave — before describing what you filled in. Never ignore it.",
    ]
    notes = spec.get("notes") or []
    if notes:
        lines.append("Adjustments already made — mention the relevant ones to the user in your next message:")
        lines.extend(f"- {n}" for n in notes)
    if spec.get("ambiguity_note"):
        lines.append(
            f"Unclear from the user's request: {spec['ambiguity_note']} Prefer a sensible default; "
            "ask AT MOST one clarification (via ask_clarification) if it truly matters."
        )
    if spec.get("scoring_formula_hint"):
        lines.append(
            f"The user stated a scoring formula: {spec['scoring_formula_hint']!r} — if it isn't already set "
            "in the draft, apply it with update_scoring_formula."
        )
    return "\n".join(lines)


_CHANGE_INTENT = re.compile(
    r"\b(remove|delete|drop|get rid|rename|replace|swap|move|regroup|merge|combine|split|without|instead|"
    r"exclude|change|re-?weigh\w*|weigh\w*|weights?|priorit\w*|important|importance|increase|decrease|raise|"
    r"lower|reduce|rewrite|reword|update|adjust|edit|modify|tweak|guidelines?|thresholds?|criteria|bump|"
    r"make|set|scale|rebalance|add)\b",
    re.IGNORECASE,
)
# A reference to a KPI the user does not name ("make THAT KPI 8%"): the assistant's previous message is
# then part of the verification window, so the name can be matched there.
_ANAPHORA = re.compile(r"\b(that|this|it|the (?:last|previous|same|above))\b", re.IGNORECASE)


def _verified_user_changes(declared: set[str], messages: list[ChatTurn]) -> tuple[set[str], set[str]]:
    """Server-side verification of the model's `user_requested_kpi_changes` claim — the model's
    flag alone never suffices (deterministic checks beat model self-attestation for
    authorization-style constraints; an LLM judge adds cost and its own failure modes for a
    property this exactly checkable). A declared KPI name counts as user-requested only if
    (a) the user's LATEST message mentions that KPI by name and contains a change-intent word
    (remove/rename/reweight/rewrite/...), or (b) that message is a short reply (<= 80 chars,
    e.g. "yes") to the assistant's immediately preceding message that mentions the KPI and a
    change-intent word — the ask_clarification confirmation path. Returns (verified,
    unverified)."""
    last_user = max((i for i, t in enumerate(messages) if t["role"] == "user"), default=None)
    if last_user is None or not declared:
        return set(), set(declared)
    window = messages[last_user]["content"]
    if len(window) <= 80 or _ANAPHORA.search(window):
        previous = next((t for t in reversed(messages[:last_user]) if t["role"] == "assistant"), None)
        if previous is not None:
            window = f"{previous['content']} {window}"
    if not _CHANGE_INTENT.search(window):
        return set(), set(declared)
    padded = f" {_normalize_kpi_name_for_dedup(window)} "
    verified = {n for n in declared if (norm := _normalize_kpi_name_for_dedup(n)) and f" {norm} " in padded}
    return verified, declared - verified


def _pinned_violations(
    pinned: list[dict[str, Any]], new_kpis: list[KpiDraft], allowed_changes: set[str]
) -> list[str]:
    """Which pinned (user-specified) KPIs a candidate KPI list drops, renames, re-parents —
    or whose USER-SUPPLIED weight/guidelines it silently changes — excluding names in
    `allowed_changes` (already verified against the user's messages — see
    `_verified_user_changes`). Compared by exact name: "preserve exactly" is the contract.
    Only values the user themselves supplied are protected (`weight`/`guidelines_hash` are
    set on a pin only then); model-filled weights/guidelines stay freely editable, which is
    also how leaf weights get rebalanced to sum to 100 when KPIs are added."""
    by_name = {k.name: k for k in new_kpis}
    problems: list[str] = []
    for p in pinned:
        if p["name"] in allowed_changes:
            continue
        current = by_name.get(p["name"])
        if current is None:
            problems.append(f'"{p["name"]}" was removed or renamed')
            continue
        if current.parent_name != p.get("parent_name"):
            problems.append(f'"{p["name"]}" was moved from "{p.get("parent_name")}" to "{current.parent_name}"')
        if p.get("weight") is not None and abs((current.weight or 0.0) - float(p["weight"])) > 0.05:
            problems.append(
                f'the user-supplied weight of "{p["name"]}" ({float(p["weight"]):g}) was changed to '
                f"{(current.weight or 0.0):g}"
            )
        if p.get("guidelines_hash") and _guidelines_hash(
            {k: v.model_dump(mode="json") for k, v in current.guidelines.items()}
        ) != p["guidelines_hash"]:
            problems.append(f'the user-supplied guidelines of "{p["name"]}" were rewritten')
    return problems


def _count_enrichment_chunks(spec: UserSpec, include_weighting: bool = True) -> int:
    """How many preparation agents `_prepare_pinned_kpis` will dispatch (enrichment chunks, plus
    the weighting step's research agent) == how many `research_agent_N` actor slots they occupy
    in the live trace, so hybrid mode can number the open-ended fan-out's agents after them
    without actor collisions."""
    leaves = _user_leaf_names(spec.kpis)
    names = [n["name"] for n in spec.kpis if n["name"] in leaves and n["included_in_scoring"]]
    return len(_research_groups(names))


async def _classify_with_events(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    conversation_context: str,
    session_id: str,
    turn_started_at: datetime | None,
    jev_client: JevClientProtocol | None = None,
    user_text: str | None = None,
) -> UserSpec | None:
    """`_classify_request` plus live-trace events. NEVER raises — any failure is treated as
    open-ended (the pre-existing behaviour).

    The expensive main-model extraction only runs when the cheap cascade in
    `request_routing.route_request` (heuristic gate -> Jev `choice` / small-model option pick)
    does not confidently say "open_ended" — see that module. `user_text` is the user's own
    message the cascade inspects (defaults to the whole conversation context)."""
    await emit_turn_event(
        session_id, turn_started_at, "master", "classifying",
        "Checking whether you've already specified your own KPIs…",
    )
    started = time.monotonic()
    text = user_text if user_text is not None else conversation_context
    # Fast path: a clearly structured list of plain KPI names is parsed deterministically — the
    # GLM-5 extraction re-types every name as tool output and took 1-2 minutes for 35 KPIs.
    raw_list = parse_structured_kpi_list(text)
    if raw_list:
        kpis, notes = _normalize_user_kpis(raw_list)
        if kpis:
            logger.info("research_kpis: parsed %d KPI(s) deterministically (no model extraction).", len(kpis))
            spec = UserSpec(
                mode="user_specified", kpis=kpis, reasoning="deterministic structured-list parse", notes=notes
            )
            await emit_turn_event(
                session_id, turn_started_at, "master", "mode_detected",
                f"Detected your KPI list — keeping your {len(kpis)} KPI(s) exactly as given and filling in the rest.",
            )
            await _emit_phase(session_id, turn_started_at, "classify", started)
            return spec
    decision = await route_request(text, bedrock=bedrock, jev_client=jev_client)
    logger.info("research_kpis: request routing -> %s (via %s).", decision.mode, decision.source)
    if decision.skip_extraction:
        await emit_turn_event(
            session_id, turn_started_at, "master", "mode_detected",
            "No explicit KPI list detected — designing and researching KPIs for you.",
        )
        return None
    spec = None
    for attempt in (1, 2):
        try:
            spec = await asyncio.to_thread(_classify_request, bedrock, model_id, conversation_context)
            break
        except BedrockUnavailableError:
            # The cheap gate/router already saw list-like structure, so quietly treating this as
            # open-ended would invent KPIs over the user's own list. Retry once (throttling/blip); if
            # Bedrock is still unavailable nothing else in this turn can work either: fail the turn
            # (the user can retry) rather than redesign their KPIs.
            if attempt == 2:
                raise
            logger.warning("research_kpis: classification hit a Bedrock error; retrying once.", exc_info=True)
        except Exception:  # noqa: BLE001 — any other classification failure must not break the turn
            logger.warning("research_kpis: request classification failed; treating as open-ended.", exc_info=True)
            break
    n = len(spec.kpis) if spec is not None else 0
    if spec is None:
        message = "No explicit KPI list detected — designing and researching KPIs for you."
    elif spec.mode == "user_specified":
        message = f"Detected your KPI list — keeping your {n} KPI(s) exactly as given and filling in the rest."
    else:
        message = f"Detected {n} KPI(s) from you — keeping them as given and researching complementary ones."
    await emit_turn_event(session_id, turn_started_at, "master", "mode_detected", message)
    await _emit_phase(session_id, turn_started_at, "classify", started)
    return spec


# --- Early scorecard header (name/purpose/domain/audience/target score) --------------------------
#
# In user-specified/hybrid mode the header used to be filled only by the FINAL propose_kpis step, so
# any slowdown, timeout, cancel or error before it left the live preview with 35 KPIs but an
# "Untitled scorecard" and empty purpose/audience/target. The header is now filled right after mode
# detection: user-given values win, then one small bounded call, then a deterministic derivation from
# the message — so it is never empty. It is also published as an interim draft (below) the session GET
# can read before the graph node finishes.

HEADER_FIELDS = ("name", "purpose", "domain", "audience", "target_score")
EARLY_HEADER_TIMEOUT_SECONDS = 20
DEFAULT_TARGET_SCORE = 8.0

SET_SCORECARD_HEADER_TOOL = ToolSpec(
    name="set_scorecard_header",
    description=(
        "Infer the scorecard header from the user's message: a short scorecard name, a one-sentence purpose "
        "(why the scorecard is needed), the domain, the audience who will use it, and a realistic target "
        "score (0-10). Never ask; always infer."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "purpose": {"type": "string"},
            "domain": {"type": "string"},
            "audience": {"type": "string"},
            "target_score": {"type": "number", "minimum": 0, "maximum": 10},
        },
        "required": ["name", "purpose", "domain", "audience", "target_score"],
    },
)

_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_USE_CASE_FOR_RE = re.compile(
    r"\bfor\s+(?:our|the|my|a|an)?\s*([A-Za-z][\w &/'-]{2,60}?)(?=\s+(?:organi[sz]ation|team|department|company|"
    r"business|group)\b|[.,:;\n]|$)",
    re.IGNORECASE,
)


def _use_case_text(message: str) -> str:
    """The prose of the user's message that is not part of a KPI list or the trailing question."""
    keep = [ln.strip() for ln in message.splitlines() if not _BULLET_LINE_RE.match(ln)]
    prose = " ".join(x for x in keep if x)
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", prose) if x.strip()]
    use = [x for x in sentences if not x.endswith("?") and not re.search(r"\b(kpis?|metrics?)\b.*:\s*$", x, re.I)]
    return " ".join(use[:2])[:400]


def _derive_header(message: str) -> dict[str, Any]:
    """Deterministic header from the user's message (no model): used when the small call fails."""
    use_case = _use_case_text(message)
    match = _USE_CASE_FOR_RE.search(use_case)
    subject = " ".join(match.group(1).split()).title() if match else ""
    if subject:
        name = f"{subject} Quality Scorecard"
    else:
        words = re.sub(r"[^\w\s&/'-]", " ", use_case).split()[:5]
        name = (" ".join(words).title() + " Scorecard") if words else "Quality Scorecard"
    return {
        "name": name[:80],
        "purpose": use_case or "Measure and improve quality using the KPIs defined by the user.",
        "domain": (subject or "General")[:80],
        "audience": "Managers, team leads and quality reviewers",
        "target_score": DEFAULT_TARGET_SCORE,
    }


def _clean_header(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("name", "purpose", "domain", "audience"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = " ".join(value.split())
    target = raw.get("target_score")
    if isinstance(target, int | float) and not isinstance(target, bool) and 0 <= float(target) <= 10:
        out["target_score"] = float(target)
    return out


def _header_call(bedrock: BedrockClientProtocol, message: str, kpi_names: list[str]) -> dict[str, Any]:
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": message[:3000]}]}],
        system=(
            "Infer a scorecard header from the user's message. Their KPIs: "
            + "; ".join(kpi_names[:60])
            + ". Call `set_scorecard_header` exactly once."
        ),
        tools=[SET_SCORECARD_HEADER_TOOL],
        force_tool_use=True,
        model_id=get_settings().bedrock_judge_model_id,
    )
    if result.is_tool_use and result.tool_name == "set_scorecard_header":
        return _clean_header(result.tool_input or {})
    return {}


async def _fill_header(
    bedrock: BedrockClientProtocol | None,
    message: str,
    kpi_names: list[str],
    given: dict[str, Any],
) -> dict[str, Any]:
    """Complete header: `given` (the user's own values, never overwritten) + one small bounded call
    + deterministic fallback for whatever is still missing. Never raises, never leaves a field empty."""
    out = {k: v for k, v in given.items() if k in HEADER_FIELDS and v not in (None, "")}
    missing = [k for k in HEADER_FIELDS if k not in out]
    if missing and bedrock is not None:
        try:
            got = await asyncio.wait_for(
                asyncio.to_thread(_header_call, bedrock, message, kpi_names), timeout=EARLY_HEADER_TIMEOUT_SECONDS
            )
            for key in missing:
                if key in got:
                    out[key] = got[key]
        except Exception:  # noqa: BLE001 — incl. timeouts: fall back to the deterministic derivation
            logger.warning("early scorecard header call failed; deriving it from the message.", exc_info=True)
    if any(k not in out for k in HEADER_FIELDS):
        derived = _derive_header(message)
        for key in HEADER_FIELDS:
            out.setdefault(key, derived[key])
    return out


# Interim drafts: the LangGraph checkpoint is only written when a node FINISHES, so while the long
# `research_kpis` node runs (and if it times out / is cancelled / fails) the session GET would see no
# draft. The early header and the pinned KPIs are published here as soon as they exist; the GET
# overlays them onto whatever the checkpoint has (see `_overlay_interim`). Process-local and bounded.
_interim_drafts: dict[str, dict[str, Any]] = {}
_MAX_INTERIM_DRAFTS = 200


def _publish_interim(session_id: str, draft: dict[str, Any]) -> None:
    _interim_drafts.pop(session_id, None)
    _interim_drafts[session_id] = draft
    while len(_interim_drafts) > _MAX_INTERIM_DRAFTS:
        _interim_drafts.pop(next(iter(_interim_drafts)))


def _overlay_interim(session_id: str, draft: dict[str, Any] | None) -> dict[str, Any]:
    """The checkpointed draft, with empty header fields / missing KPIs filled from the interim one."""
    draft = dict(draft or {})
    interim = _interim_drafts.get(session_id)
    if not interim:
        return draft
    for key in HEADER_FIELDS:
        if draft.get(key) in (None, "") and interim.get(key) not in (None, ""):
            draft[key] = interim[key]
    if not draft.get("kpis") and interim.get("kpis"):
        draft["kpis"] = interim["kpis"]
    return draft


def _pinned_only_update(spec: UserSpec, pinned: PinnedKpis, draft: ScorecardDraft) -> dict[str, Any]:
    """The `research_kpis` state update for a session whose KPIs are all user-specified (no
    open-ended fan-out ran): seeds `draft.kpis` with the user's KPIs, applies any scalar
    fields/formula they stated, and records the pinned set + notes for `propose_kpis`."""
    notes = list(pinned.notes)
    draft_dict = {**draft.model_dump(mode="json"), "kpis": pinned.kpis}
    try:
        ScorecardDraft.model_validate(draft_dict)
    except ValidationError:
        logger.warning("research_kpis: user-specified KPI set failed ScorecardDraft validation.", exc_info=True)
        draft_dict = draft.model_dump(mode="json")  # still pinned in state; propose_kpis re-enters them
    draft_dict = _apply_user_spec_to_draft(draft_dict, spec, notes, pinned)
    note = (
        f"[request analysis] the user specified {len(pinned.kpis)} KPI(s) themselves — kept exactly as given; "
        "only missing guidelines/weights were filled in."
        + (" " + " ".join(notes) if notes else "")
    )
    return {
        "research_done": True,
        "research_findings": [asdict(f) for f in pinned.findings] if pinned.findings else None,
        "draft": draft_dict,
        "user_spec": _user_spec_state(spec, pinned.kpis, notes),
        "messages": [{"role": "assistant", "content": note}],
    }


def _web_search_usable(client: WebSearchClientProtocol | None) -> bool:
    """Whether it's worth spending the extra `decide_categories` Bedrock call at all.
    Unlike propose_kpis's own ad hoc `web_search` tool offering (unchanged — still offered
    whenever `client is not None`, since a misconfigured real client's own `.search()`
    gracefully returns `[]` per its documented contract, and offering it costs nothing
    extra there), `research_kpis` unconditionally spends one whole Bedrock call up front
    just to decide categories — not worth it if search can't possibly return anything.
    `AgentCoreWebSearchClient` exposes `is_configured` for exactly this; a client that
    doesn't expose it (e.g. `FakeWebSearchClient`/`FakeBedrockClient` in tests, or any
    other `WebSearchClientProtocol` implementation) is assumed usable."""
    if client is None:
        return False
    return getattr(client, "is_configured", True)


def _leaf_total(kpis: list[dict[str, Any]]) -> int:
    """How many KPIs the user would count: every node nothing else is nested under."""
    parents = {k.get("parent_name") for k in kpis if k.get("parent_name")}
    return sum(1 for k in kpis if k["name"] not in parents)


def _format_kpi_target_for_prompt(kpi_target: dict[str, Any] | None) -> str:
    """The REQUESTED KPI COUNT block woven into propose_kpis's system prompt, so the model's
    reconciliation/explanations stay consistent with what the pipeline actually delivered and
    it tells the user (never silently) when fewer than requested exist."""
    if not kpi_target or not kpi_target.get("requested"):
        return ""
    requested, delivered, note = kpi_target["requested"], kpi_target.get("delivered"), kpi_target.get("note")
    if delivered is None:
        return (
            f"REQUESTED KPI COUNT: the user asked for about {requested} KPIs. "
            f"Aim for {requested} (+/-{KPI_TARGET_TOLERANCE}) "
            "distinct, genuinely valuable KPIs; if that many do not make sense for this use case, deliver fewer and "
            "EXPLICITLY tell the user why in assistant_message. Never pad with weak or duplicate KPIs and never "
            "silently truncate."
        )
    lines = [
        f"REQUESTED KPI COUNT: the user asked for about {requested} KPIs; the draft currently has {delivered}. "
        "Do not add or remove KPIs to hit the number unless the user asks — the research step already sized it."
    ]
    if note:
        lines.append(f"You MUST tell the user this in your assistant_message (in your own words): {note}")
    return "\n".join(lines)


async def _research_kpis_impl(
    state: BuilderState, config: RunnableConfig, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The master/orchestrator step: given the user's stated domain/purpose so far, decides
    (one Bedrock call, see `_decide_categories`) which named KPI CATEGORIES this domain
    warrants, then runs one instance of the ONE `_run_research_agent` worker PER CATEGORY,
    CONCURRENTLY via `asyncio.gather` (genuine parallel execution — every category's
    Bedrock + web_search calls are in flight together, not one after another), and
    consolidates the structured findings — already organized by category — into state for
    `propose_kpis` to ground its proposal in. See the module comment above `MAX_CATEGORIES`
    for the full category = Level-1 KpiDraft / KPI = Level-2 KpiDraft(parent_name=category)
    design this orchestrator builds.

    **Iterative / multi-round research**: after round 1's fan-out and merge, if
    `MAX_RESEARCH_ROUNDS` (2) hasn't been reached yet, the master runs ONE real confidence
    check (`_assess_research_coverage` — a genuine Bedrock judgment grounded in the actual
    consolidated findings/categorized KPIs gathered so far, never a hardcoded heuristic)
    asking whether coverage is genuinely sufficient or whether a real, distinct category/gap
    remains. If the model identifies a real gap AND proposes categories for it (new names,
    or an existing name to deepen), a SECOND bounded round of the same concurrent fan-out
    runs — see `MAX_RESEARCH_ROUNDS`'s own docstring for exactly how "new category" vs.
    "deepen an existing one" composes for free through `_merge_research_kpi_batches`
    grouping by name.

    Runs at most ONCE per session (guarded by `research_done`, set on every path out of this
    node) — see `MAX_RESEARCH_ROUNDS`'s own docstring for why this (potentially multi-round)
    node still can't affect MAX_LLM_TURNS_PER_HUMAN_TURN. A no-op (research_done=True, no
    findings) when no `web_search_client` is wired in (mirrors propose_kpis's own gating),
    or when round 1's category decision itself fails/returns nothing — propose_kpis then
    simply proceeds exactly as it did before this feature existed (its own ad hoc
    web_search + the model's own knowledge), which is the graceful-degradation path
    required when Bedrock/web_search is unavailable."""
    if state.get("research_done"):
        return {}

    configurable = config.get("configurable", {})
    bedrock: BedrockClientProtocol | None = configurable.get("bedrock_client")
    model_id: str | None = configurable.get("chat_model_id")
    web_search_client: WebSearchClientProtocol | None = configurable.get("web_search_client")
    jev_client: JevClientProtocol | None = configurable.get("jev_client")
    session_id: str = state["session_id"]
    turn_started_at: datetime | None = configurable.get("turn_started_at")

    if bedrock is None:
        return {"research_done": True}

    draft = ScorecardDraft.model_validate(state["draft"])
    conversation_context = _conversation_context_text(state.get("messages") or [])
    # A requested KPI count ("give me 35 KPIs") — deterministic extraction, zero model calls.
    requested_count = extract_kpi_target(_last_user_message(state.get("messages") or []))
    if meta is not None and requested_count:
        meta["kpi_target"] = {"requested": requested_count, "delivered": None, "note": None}

    # --- Request classification (see the "User-specified KPI mode" section above): decides
    # whether the user already defined their own KPIs. Runs BEFORE the web-search gate
    # because user-specified mode works without web search (guidelines then come from the
    # model's own knowledge). `spec is None` == open-ended == every line below this block
    # behaves exactly as it did before the feature existed.
    spec = await _classify_with_events(
        bedrock, model_id, conversation_context, session_id, turn_started_at, jev_client,
        user_text=_last_user_message(state.get("messages") or []),
    )
    pinned_task: asyncio.Task[PinnedKpis] | None = None
    pinned: PinnedKpis | None = None
    enrichment_actors = 0
    if spec is not None:
        enrichment_actors = _count_enrichment_chunks(spec)
        # Header first (cheap, bounded): published right away so the preview shows it within seconds,
        # and applied to the draft even if every later step fails.
        header = await _fill_header(
            bedrock, _last_user_message(state.get("messages") or []), [n["name"] for n in spec.kpis],
            {**{k: v for k, v in draft.model_dump(mode="json").items() if k in HEADER_FIELDS}, **spec.scalars},
        )
        spec.scalars = {**header, **spec.scalars}
        def publish_pinned(details: dict[str, dict[str, Any]]) -> None:
            """Interim draft = the user's KPIs with whatever guidelines exist so far (user-supplied
            or already written), so the live preview fills in as guideline calls complete."""
            _publish_interim(
                session_id,
                {
                    **draft.model_dump(mode="json"),
                    **spec.scalars,
                    "kpis": [
                        KpiDraft(
                            name=n["name"], level=n["level"], parent_name=n["parent_name"],
                            included_in_scoring=n["included_in_scoring"],
                            guidelines=n["guidelines"] or details.get(n["name"], {}).get("guidelines") or {},
                        ).model_dump(mode="json")
                        for n in spec.kpis
                    ],
                },
            )

        publish_pinned({})
        pinned_task = asyncio.create_task(
            _prepare_pinned_kpis(
                spec, bedrock, web_search_client, model_id,
                session_id=session_id, turn_started_at=turn_started_at,
                conversation_context=conversation_context, jev_client=jev_client,
                on_progress=publish_pinned,
            )
        )
        if spec.mode == "user_specified":
            # Authoritative user KPIs: NO category/KPI invention fan-out at all. A count in the text
            # ("35 KPIs") just describes the user's own list — it must not steer the model to add/trim.
            if meta is not None:
                meta.pop("kpi_target", None)
            update = _pinned_only_update(spec, await pinned_task, draft)
            _publish_interim(session_id, update["draft"])
            return update

    async def finish_without_fanout() -> dict[str, Any]:
        if spec is not None and pinned_task is not None:  # hybrid, but no research to add — keep the user's KPIs
            if meta is not None:
                meta.pop("kpi_target", None)
            return _pinned_only_update(spec, await pinned_task, draft)
        return {"research_done": True}

    # Sizing: a requested count N (hybrid: minus the user's own KPIs) drives the category count and
    # each agent's quota (see `_plan_fanout`); no count = dynamic.
    pinned_leaf_count = len(_user_leaf_names(spec.kpis)) if spec is not None else 0
    research_target = requested_count - pinned_leaf_count if requested_count else None
    if research_target is not None and research_target <= 0:
        return await finish_without_fanout()  # the user's own KPIs already meet/exceed the requested count
    plan = _plan_fanout(research_target)
    search_usable = _web_search_usable(web_search_client)
    max_searches = MAX_SEARCH_CALLS_PER_RESEARCH_AGENT if search_usable else 0
    if not search_usable and not (plan.target and plan.target >= FANOUT_WITHOUT_SEARCH_MIN_TARGET):
        return await finish_without_fanout()

    await emit_turn_event(
        session_id, turn_started_at, "master", "started",
        "Reviewing what you've said so far to plan KPI categories…"
        if spec is None
        else "Planning complementary KPI categories around the KPIs you gave…",
    )

    planning_context = conversation_context
    if spec is not None:
        planning_context += (
            "\n\n[The user already defined these KPIs, so they are covered — plan categories for COMPLEMENTARY "
            "coverage only, never re-proposing them]: " + "; ".join(n["name"] for n in spec.kpis)
        )

    header_task: asyncio.Task[dict[str, Any]] | None = None
    if spec is None:  # open-ended: the header (incl. audience) is filled concurrently with planning
        header_task = asyncio.create_task(
            _fill_header(
                bedrock, _last_user_message(state.get("messages") or []), [],
                {k: v for k, v in draft.model_dump(mode="json").items() if k in HEADER_FIELDS},
            )
        )
    try:
        categories = await _decide_categories_with_gate(
            bedrock, model_id, draft, planning_context, jev_client, session_id, turn_started_at,
            sizing_hint=_sizing_hint(plan),
        )
    except Exception:  # noqa: BLE001 — category decision failing must not break the turn either
        logger.warning("research_kpis: failed to decide categories; skipping fan-out.", exc_info=True)
        await emit_turn_event(
            session_id, turn_started_at, "master", "error",
            "Could not plan KPI categories — proceeding without dedicated research.",
        )
        return await finish_without_fanout()

    if not categories:
        logger.info("research_kpis: model decided no dedicated category/research fan-out was needed.")
        await emit_turn_event(
            session_id, turn_started_at, "master", "deciding_categories",
            "Determined this domain doesn't need a dedicated category structure or research fan-out.",
        )
        return await finish_without_fanout()

    # --- Multi-round loop (bounded by MAX_RESEARCH_ROUNDS — see that constant's own
    # docstring for the full latency/cost/MAX_LLM_TURNS_PER_HUMAN_TURN reasoning). Round 1
    # always runs with the categories `_decide_categories` just chose above; every
    # subsequent round (if any) runs with the categories `_assess_research_coverage`
    # proposed for a genuine identified gap (new names, or an existing name to deepen).
    # `all_findings`/`all_categories_covered`/`category_meta` accumulate across every round
    # so far, so the merge step (`_merge_research_kpi_batches`) and the confidence check
    # both always see the FULL picture, not just the latest round. `category_meta` (keyed
    # by category name) is what lets the merge step recover each surviving category's
    # `initial_weight` guess without needing to re-derive it from the findings themselves.
    all_findings: list[ResearchFinding] = []
    all_categories_covered: list[dict[str, Any]] = []
    category_meta: dict[str, dict[str, Any]] = {}
    merged_kpis: list[dict[str, Any]] = []
    updated_draft_dict = draft.model_dump(mode="json")
    total_proposed_before_merge = 0
    round_num = 1
    judged: dict[frozenset[str], bool] = {}  # borderline duplicate pairs the small model has judged

    async def merge_all() -> list[dict[str, Any]]:
        """Merge every finding so far; borderline name pairs are first judged ONCE (cached) by the
        small model so distinct KPIs that merely share words are not dropped (`_dup_relation`)."""
        names = [str(k.get("name") or "") for k in _interleave_findings(all_findings)]
        await _judge_duplicate_pairs(bedrock, _borderline_pairs(names), judged)
        return _merge_research_kpi_batches(
            all_findings, category_meta,
            pinned=pinned.kpis if pinned is not None else None,
            same_pairs={pair for pair, same in judged.items() if same},
        )

    def commit_merge(candidate_kpis: list[dict[str, Any]]) -> None:
        nonlocal updated_draft_dict, merged_kpis
        if not candidate_kpis:
            return
        candidate_draft_dict = {**draft.model_dump(mode="json"), "kpis": candidate_kpis}
        try:
            ScorecardDraft.model_validate(candidate_draft_dict)  # sanity check before committing to state
        except ValidationError:
            logger.warning(
                "research_kpis: merged KPI batch failed ScorecardDraft validation; keeping last good.", exc_info=True
            )
            return
        updated_draft_dict, merged_kpis = candidate_draft_dict, candidate_kpis

    while True:
        for c in categories:
            category_meta[c["name"]] = {**category_meta.get(c["name"], {}), **c}

        logger.info(
            "research_kpis: round %d dispatching %d research agent(s) concurrently (one per category): %s",
            round_num, len(categories), categories,
        )
        if round_num == 1:
            await emit_turn_event(
                session_id, turn_started_at, "master", "deciding_categories",
                f"Decided on {len(categories)} KPI categor{'y' if len(categories) == 1 else 'ies'}: "
                + "; ".join(f'"{c["name"]}"' for c in categories),
                round=round_num,
            )
        else:
            # Distinct wording (never identical to round 1's message) so the live trace
            # UI/logs make clear this round exists BECAUSE round 1 wasn't judged
            # sufficient — see `_assess_research_coverage`'s reasoning, echoed here too.
            await emit_turn_event(
                session_id, turn_started_at, "master", "deciding_categories",
                f"Round {round_num}: investigating {len(categories)} categor"
                f"{'y' if len(categories) == 1 else 'ies'} to address a gap round 1 didn't "
                "cover: " + "; ".join(f'"{c["name"]}"' for c in categories),
                round=round_num,
            )

        raw_findings = await asyncio.gather(
            *(
                _run_research_agent(
                    c["name"],
                    c["focus"],
                    bedrock,
                    web_search_client,
                    model_id,
                    session_id=session_id,
                    turn_started_at=turn_started_at,
                    actor=f"research_agent_{enrichment_actors + i + 1}",
                    round_num=round_num,
                    jev_client=jev_client,
                    conversation_context=conversation_context,
                    avoid_kpi_names=[n["name"] for n in spec.kpis] if spec is not None else None,
                    want_kpis=plan.per_category,
                    max_searches=max_searches,
                )
                for i, c in enumerate(categories)
            ),
            return_exceptions=True,  # belt-and-suspenders — _run_research_agent already never raises
        )

        round_findings: list[ResearchFinding] = []
        for item in raw_findings:
            if isinstance(item, BaseException):
                logger.warning("research_kpis: a research agent raised unexpectedly; excluding it.", exc_info=item)
                continue
            round_findings.append(item)

        usable = [f for f in round_findings if f.has_content()]
        logger.info(
            "research_kpis: round %d — %d/%d agent(s) returned usable findings (%d degraded/empty excluded).",
            round_num, len(usable), len(categories), len(round_findings) - len(usable),
        )

        all_findings.extend(usable)
        all_categories_covered.extend(categories)
        total_proposed_before_merge += sum(len(f.proposed_kpis) for f in usable)

        # --- Merge point — see _merge_research_kpi_batches's own docstring. Re-run over
        # ALL findings accumulated so far (not just this round's), so a round-2 agent's
        # near-duplicate of a round-1 KPI is caught by the same dedup pass, a "deepen"
        # round's new KPIs join that same category's round-1 children, and the whole pool
        # is renormalized together — never two independently-normalized pools bolted
        # together.
        if pinned_task is not None and pinned is None:
            # Hybrid: the user's KPIs finished enriching concurrently with the fan-out above.
            pinned = await pinned_task
            pinned_draft = {**draft.model_dump(mode="json"), "kpis": pinned.kpis}
            try:
                ScorecardDraft.model_validate(pinned_draft)
                updated_draft_dict, merged_kpis = pinned_draft, list(pinned.kpis)
            except ValidationError:
                logger.warning("research_kpis: pinned KPI set failed ScorecardDraft validation.", exc_info=True)
        commit_merge(await merge_all())

        categories_in_merge = sorted({k["name"] for k in merged_kpis if k.get("level") == 1})
        logger.info(
            "research_kpis: round %d — merged %d proposed KPI(s) from %d agent batch(es) so far into "
            "%d categorized KPI(s) across %d categor%s: %s",
            round_num, total_proposed_before_merge, len(all_findings), len(merged_kpis),
            len(categories_in_merge), "y" if len(categories_in_merge) == 1 else "ies",
            [k["name"] for k in merged_kpis],
        )

        reached_cap = round_num >= MAX_RESEARCH_ROUNDS
        assessment: CoverageAssessment | None = None
        if not reached_cap:
            # Only spend the extra confidence-check Bedrock call when there's actually a
            # possible further round to launch — skipping it entirely once the cap is hit
            # is itself part of respecting the latency budget (see MAX_RESEARCH_ROUNDS).
            await emit_turn_event(
                session_id, turn_started_at, "master", "assessing_coverage",
                f"Assessing whether round {round_num}'s research covers this domain well enough…",
                round=round_num,
            )
            try:
                assessment = await asyncio.to_thread(
                    _assess_research_coverage,
                    bedrock,
                    model_id,
                    ScorecardDraft.model_validate(updated_draft_dict),
                    conversation_context,
                    all_findings,
                    all_categories_covered,
                    round_num,
                    _sizing_hint(plan, have=_leaf_total(merged_kpis))
                    if plan.target
                    else _dynamic_coverage_hint(_leaf_total(merged_kpis), len(category_meta)),
                )
            except Exception:  # noqa: BLE001 — a failed confidence check must never break the turn
                logger.warning(
                    "research_kpis: coverage assessment failed after round %d; proceeding with what we have.",
                    round_num, exc_info=True,
                )
                await emit_turn_event(
                    session_id, turn_started_at, "master", "error",
                    "Could not assess research coverage — proceeding with what's been found so far.",
                    round=round_num,
                )

        will_continue = (
            not reached_cap
            and assessment is not None
            and not assessment.sufficient
            and bool(assessment.next_categories)
        )

        if will_continue:
            assert assessment is not None  # for mypy/readability — guarded by will_continue above
            await emit_turn_event(
                session_id, turn_started_at, "master", "completed",
                f"Round {round_num} complete — {len(usable)}/{len(categories)} finding(s), "
                f"{len(merged_kpis)} KPI(s) merged across {len(categories_in_merge)} categor"
                f"{'y' if len(categories_in_merge) == 1 else 'ies'} so far. Not yet sufficient: "
                f"{assessment.reasoning or 'a real gap remains'} — starting round {round_num + 1}.",
                round=round_num,
            )
            categories = assessment.next_categories
            round_num += 1
            continue

        if reached_cap:
            stop_reason = f"reached the {MAX_RESEARCH_ROUNDS}-round research cap"
        elif assessment is not None and assessment.sufficient:
            stop_reason = f"coverage assessed sufficient ({assessment.reasoning or 'no further gap identified'})"
        else:
            stop_reason = "proceeding with what's been found so far"
        await emit_turn_event(
            session_id, turn_started_at, "master", "completed",
            f"Research phase complete after {round_num} round(s) ({stop_reason}) — consolidated "
            f"{len(all_findings)} finding(s) across {len(all_categories_covered)} categor"
            f"{'y' if len(all_categories_covered) == 1 else 'ies'}; merged {total_proposed_before_merge} "
            f"proposed KPI(s) into {len(merged_kpis)} categorized KPI(s) across {len(categories_in_merge)} "
            f"categor{'y' if len(categories_in_merge) == 1 else 'ies'} for review.",
            round=round_num,
        )
        break

    # --- Count reconciliation (only when the user asked for a specific number) -------------------
    if requested_count and merged_kpis:
        deepen_rounds = 0
        tried_new_categories = False
        while (
            _leaf_total(merged_kpis) < requested_count - KPI_TARGET_TOLERANCE and deepen_rounds < MAX_DEEPEN_ROUNDS
        ):
            deepen_rounds += 1
            have = _leaf_total(merged_kpis)
            deficit = requested_count - have
            cat_names = [c for c in category_meta if any(k.get("parent_name") == c for k in merged_kpis)]
            if not cat_names:
                break
            ask = max(2, math.ceil(deficit * 1.25 / len(cat_names)))
            await emit_turn_event(
                session_id, turn_started_at, "master", "deepening",
                f"{have} of the {requested_count} KPIs you asked for so far — deepening {len(cat_names)} "
                f"categor{'y' if len(cat_names) == 1 else 'ies'} (pass {deepen_rounds}/{MAX_DEEPEN_ROUNDS})…",
                round=round_num,
            )
            avoid = [k["name"] for k in merged_kpis]
            reps = {f.category: f for f in all_findings}
            outcomes = await asyncio.gather(
                *(
                    _propose_category_kpis(
                        bedrock, model_id, category=c, focus=str(category_meta[c].get("focus") or c),
                        finding=reps.get(c) or ResearchFinding(category=c), avoid_names=avoid, want=ask,
                        avoid_is_user=False, conversation_context=conversation_context,
                        session_id=session_id, turn_started_at=turn_started_at,
                    )
                    for c in cat_names
                ),
                return_exceptions=True,
            )
            for c, outcome in zip(cat_names, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    logger.warning("deepen pass: category %r failed.", c, exc_info=outcome)
                elif outcome:
                    all_findings.append(ResearchFinding(category=c, proposed_kpis=outcome))
            commit_merge(await merge_all())
            if _leaf_total(merged_kpis) > have:
                continue
            if tried_new_categories:
                break
            tried_new_categories = True
            try:
                assessment = await asyncio.to_thread(
                    _assess_research_coverage, bedrock, model_id,
                    ScorecardDraft.model_validate(updated_draft_dict), conversation_context,
                    all_findings, all_categories_covered, round_num,
                    _sizing_hint(plan, have=_leaf_total(merged_kpis)),
                )
            except Exception:  # noqa: BLE001 — a failed assessment just ends deepening
                break
            fresh = [c for c in assessment.next_categories if c["name"] not in category_meta]
            if not fresh:
                break
            round_num += 1
            for c in fresh:
                category_meta[c["name"]] = {**c}
            all_categories_covered.extend(fresh)
            new_findings = await asyncio.gather(
                *(
                    _run_research_agent(
                        c["name"], c["focus"], bedrock, web_search_client, model_id,
                        session_id=session_id, turn_started_at=turn_started_at,
                        actor=f"research_agent_{enrichment_actors + 100 + i}", round_num=round_num,
                        jev_client=jev_client, conversation_context=conversation_context,
                        avoid_kpi_names=avoid, want_kpis=plan.per_category, max_searches=max_searches,
                    )
                    for i, c in enumerate(fresh)
                ),
                return_exceptions=True,
            )
            all_findings.extend(f for f in new_findings if not isinstance(f, BaseException) and f.has_content())
            commit_merge(await merge_all())
            if _leaf_total(merged_kpis) <= have:
                break

        delivered = _leaf_total(merged_kpis)
        if delivered > requested_count + KPI_TARGET_TOLERANCE:
            # Over target: drop the lowest-weighted RESEARCHED leaves (the user's own KPIs are never
            # pruned) — weight is the research agents' own importance judgement, and near-duplicates
            # were already judged out above — then re-merge so weights renormalize.
            pinned_names = {p["name"] for p in (pinned.kpis if pinned is not None else [])}
            parents = {k.get("parent_name") for k in merged_kpis if k.get("parent_name")}
            candidates = sorted(
                (k for k in merged_kpis if k["name"] not in parents and k["name"] not in pinned_names),
                key=lambda k: float(k.get("weight") or 0.0),
            )
            drop = {k["name"] for k in candidates[: delivered - requested_count]}
            for f in all_findings:
                f.proposed_kpis = [k for k in f.proposed_kpis if k["name"] not in drop]
            commit_merge(await merge_all())
            prune_note = (
                f"I proposed more than the {requested_count} KPIs you asked for, so I dropped the "
                f"{len(drop)} lowest-weighted ones to match."
            )
            await emit_turn_event(
                session_id, turn_started_at, "master", "completed", prune_note, round=round_num
            )
            delivered = _leaf_total(merged_kpis)
            shortfall_note = None
        elif delivered < requested_count - KPI_TARGET_TOLERANCE:
            shortfall_note = (
                f"You asked for {requested_count} KPIs, but I could identify only {delivered} distinct, "
                f"well-supported ones for this use case: after {deepen_rounds} deepening pass(es) the remaining "
                "ideas either overlapped KPIs already in the scorecard or were too weak to be useful, and I "
                f"would rather give you {delivered} solid KPIs than pad it with weak or duplicate ones."
            )
        else:
            shortfall_note = None
        if meta is not None:
            meta["kpi_target"] = {"requested": requested_count, "delivered": delivered, "note": shortfall_note}
        categories_in_merge = sorted({k["name"] for k in merged_kpis if k.get("level") == 1})

    note = (
        f"[research] investigated {len(all_categories_covered)} categor"
        f"{'y' if len(all_categories_covered) == 1 else 'ies'} across {round_num} round(s): "
        + ", ".join(c["name"] for c in all_categories_covered)
        + (
            f"; merged {len(merged_kpis)} KPI(s), organized under {len(categories_in_merge)} categor"
            f"{'y' if len(categories_in_merge) == 1 else 'ies'}: "
            + ", ".join(
                f'{k["name"]} (under "{k["parent_name"]}")' if k.get("parent_name") else k["name"]
                for k in merged_kpis
            )
            if merged_kpis
            else ""
        )
    )
    if header_task is not None:
        try:
            header = await header_task
            current_scalars = updated_draft_dict
            updated_draft_dict = {
                **updated_draft_dict,
                **{k: v for k, v in header.items() if current_scalars.get(k) in (None, "")},
            }
        except Exception:  # noqa: BLE001 — header is best-effort
            logger.warning("open-ended header fill failed.", exc_info=True)

    # Safety net: no leaf may leave research with fewer than 11 guideline levels.
    repaired_kpis, fb_names = await _repair_incomplete_leaves(
        list(updated_draft_dict.get("kpis") or []), bedrock, model_id, conversation_context,
        session_id=session_id, turn_started_at=turn_started_at,
    )
    if fb_names:
        logger.warning("research_kpis: %d KPI(s) got the marked fallback rubric: %s", len(fb_names), fb_names)
    updated_draft_dict = {**updated_draft_dict, "kpis": repaired_kpis}
    merged_kpis = repaired_kpis

    update: dict[str, Any] = {"research_done": True}
    final_findings = list(all_findings)
    if spec is not None and pinned is not None:
        notes = list(pinned.notes)
        if any(k["name"] not in {p["name"] for p in pinned.kpis} for k in merged_kpis):
            notes.append(
                "I added researched KPIs around yours, so leaf weights were rebalanced to total 100 — your KPIs keep "
                "their relative proportions."
            )
        updated_draft_dict = _apply_user_spec_to_draft(updated_draft_dict, spec, notes, pinned)
        update["user_spec"] = _user_spec_state(spec, updated_draft_dict.get("kpis") or pinned.kpis, notes)
        final_findings = [*pinned.findings, *all_findings]
        note += f" [request analysis] kept {len(pinned.kpis)} user-specified KPI(s) exactly: " + "; ".join(notes)
    update.update(
        {
            "research_findings": [asdict(f) for f in final_findings] if final_findings else None,
            "draft": updated_draft_dict,
            "messages": [{"role": "assistant", "content": note}],
        }
    )
    return update

_EDIT_REQUEST = re.compile(
    r"^(?:also,?\s*|and\s+|please\s+)*(?:(?:can|could|would|will) you\b.*\b(?:make|set|change|remove|add|rename|"
    r"move|scale|increase|decrease|reduce|raise|lower|update|adjust)\b|(?:make|set|change|remove|add|rename|move|"
    r"scale|increase|decrease|reduce|raise|lower|update|adjust)\b)",
    re.IGNORECASE,
)

_QUESTION_SENTENCE = re.compile(r"[^.?!\n]*\?")


def _extract_user_questions(text: str) -> list[str]:
    """Question sentences in a user message (a '?'-terminated sentence of a few words). KPI list lines
    are rarely questions, so a cheap regex suffices; at most 3."""
    found = [m.group(0).strip() for m in _QUESTION_SENTENCE.finditer(text or "")]
    return [q for q in found if len(q.split()) >= 3][:3]


def _real_questions(text: str) -> list[str]:
    """Questions that ask for an ANSWER — "can you make X 8%?" is an edit instruction, not a question."""
    return [q for q in _extract_user_questions(text) if not _EDIT_REQUEST.search(q)]


SIDE_ANSWER_TOOL = ToolSpec(
    name="answer_user_questions",
    description="Answer the user's side question(s) directly, in 2-4 sentences total, for a business reader.",
    input_schema={
        "type": "object",
        "properties": {"answer": {"type": "string", "description": "The answer, plain text, no preamble."}},
        "required": ["answer"],
    },
)

SIDE_QUESTION_TIMEOUT_SECONDS = 20


async def _answer_side_questions(
    bedrock: BedrockClientProtocol, model_id: str | None, context: str, questions: list[str]
) -> str | None:
    """One small bounded call (<= SIDE_QUESTION_TIMEOUT_SECONDS) answering the user's side question(s)
    from the use case + KPI list. Runs concurrently with the rest of the turn; None on any failure."""
    system = (
        "You are the scorecard-design assistant of a Quality Scorecard System. The user is designing a "
        "scorecard and also asked the question(s) below. Answer them directly and concretely in 2-4 "
        "sentences, grounded in their use case and KPIs. Call `answer_user_questions` once.\n\n"
        f"What the user said:\n{context}\n\nQuestion(s) to answer:\n" + "\n".join(f"- {q}" for q in questions)
    )
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                bedrock.converse,
                messages=[{"role": "user", "content": [{"text": "Answer the question(s) now."}]}],
                system=system, tools=[SIDE_ANSWER_TOOL], force_tool_use=True, model_id=model_id,
            ),
            timeout=SIDE_QUESTION_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — never break the turn over a side answer
        logger.warning("side-question answer failed.", exc_info=True)
        return None
    if result.is_tool_use and result.tool_name == "answer_user_questions":
        answer = str((result.tool_input or {}).get("answer") or "").strip()
        return answer or None
    return (result.text or "").strip() or None


async def research_kpis(state: BuilderState, config: RunnableConfig) -> dict[str, Any]:
    """Graph node: `_research_kpis_impl` (documented above), plus recording how many user
    messages the first-turn classification has already covered so `ingest_user_kpis` only ever
    looks at LATER messages."""
    meta: dict[str, Any] = {}
    bedrock = (config.get("configurable") or {}).get("bedrock_client")
    questions = [] if state.get("research_done") or bedrock is None else _extract_user_questions(
        _last_user_message(state.get("messages") or [])
    )
    answer_task: asyncio.Task[str | None] | None = None
    if questions:  # concurrent with the whole research/enrichment phase: adds no latency
        answer_task = asyncio.create_task(
            _answer_side_questions(
                bedrock, (config.get("configurable") or {}).get("chat_model_id"),
                _conversation_context_text(state.get("messages") or []), questions,
            )
        )
    try:
        update = await _research_kpis_impl(state, config, meta)
    except BaseException:
        if answer_task is not None:
            answer_task.cancel()
        raise
    if answer_task is not None:
        answer = await answer_task
        if answer and update:
            update = {**update, "messages": [*(update.get("messages") or []), {"role": "assistant", "content": answer}]}
    if update and not state.get("research_done"):
        update = {**update, "user_msgs_checked": sum(1 for t in state.get("messages") or [] if t["role"] == "user")}
        if meta.get("kpi_target"):
            update["kpi_target"] = meta["kpi_target"]
    return update


# A non-confirming update_draft always routes back to propose_kpis for another LLM turn
# (so the model can chain several update_draft calls, or follow one straight up with a
# clarifying question, all before a human is prompted again — see module docstring). With
# no bound this could loop forever if the model never calls ask_clarification or confirms.
# This caps how many consecutive LLM turns happen per human turn before the graph forces
# a pause, guaranteeing bounded latency/cost and that control always returns to the caller.
#
# NOTE on research_kpis (above): this counter is untouched by the research fan-out — it
# only counts propose_kpis node VISITS, and research_kpis (guarded by `research_done`) runs
# at most once per session, before the first propose_kpis visit, not once per visit. So the
# fan-out cannot itself push a human turn closer to this cutoff; MAX_LLM_TURNS_PER_HUMAN_TURN
# is left at its pre-existing value, reasoned through rather than raised reflexively. This
# holds EXACTLY as true now that research_kpis can run up to MAX_RESEARCH_ROUNDS bounded
# rounds internally (category decisions, confidence checks, and every round's agent fan-out
# all happen inside that one node invocation, still before propose_kpis's first visit) —
# see MAX_RESEARCH_ROUNDS's own docstring for the full reasoning on why research rounds and
# propose_kpis's reconciliation/confirmation turns are accounted completely separately.
MAX_LLM_TURNS_PER_HUMAN_TURN = 4
# Retries of a propose_kpis call whose output was truncated at the token limit (see propose_kpis).
MAX_TRUNCATION_RETRIES = 2

_SYSTEM_PROMPT_TEMPLATE = """You are the scorecard-design assistant for the Quality \
Scorecard System. You help a user define a reusable quality scorecard: a purpose, a \
domain, an audience, a target score (0-10), and a set of weighted KPIs (max 4 levels \
deep), each with an 11-level (0-10) qualitative + quantitative guideline.

You MUST respond on every turn by calling exactly one of the available tools — never \
reply in plain text. This does NOT mean every turn must change the draft: \
`respond_conversationally` is a real, first-class tool for genuinely talking with the \
user (answering a question, discussing/comparing options, reporting back what you found) \
without touching the draft at all — use it freely. Decide which tool fits the user's \
LATEST message like this:
- A clear decision/preference/instruction to change something ("yes, use that one", "I \
like the GDPR-based approach, add it", "rename it to X", "looks good, save it", "make \
compliance weigh more") -> `update_draft` (or `update_scoring_formula` for HOW the score \
is computed — see below). This is the ONLY way an actual change happens; describing a \
change inside `respond_conversationally` does NOT apply it.
- You are about to tell the user about specific KPIs, fields, or a draft you have decided \
on — even as an initial proposal they haven't confirmed yet (this draft is never final \
until the user confirms and you call `update_draft` with `confirmed: true` — proposing it \
into `patch` now does not lock anything in) -> `update_draft` FIRST, with those \
KPIs/fields actually included in `patch`, with your explanation written into its \
`assistant_message` field (which IS shown to the user, verbatim, exactly like \
`respond_conversationally`'s `response` — see UPDATE_DRAFT_TOOL). If you also need to ask \
the user something about what you just proposed (e.g. "hierarchical ~60 KPIs, or a flat \
~50" is a real decision only they can make) — call `update_draft` on this internal step, \
then `ask_clarification` as your VERY NEXT tool call (same human turn — you get multiple \
internal tool-call steps per human turn, see MAX_LLM_TURNS_PER_HUMAN_TURN, precisely so \
you can chain "save, then ask" like this) with that question as real chip options. Do NOT \
fold the question itself into `assistant_message` and stop there — `assistant_message` \
explains what you did; it is never where you end up asking something you actually need an \
answer to. NEVER narrate concrete, \
specific drafted content ("I've drafted a scorecard with 20 KPIs across Schedule, Budget, \
and Quality...") through `respond_conversationally` — if the content is real enough to \
describe in detail, it is real enough to persist, and `respond_conversationally` does not \
persist anything. Saying it without saving it is a bug, not a lighter-weight reply.
- An exploratory/informational question, a request to explain or compare options, or a \
reaction that doesn't itself decide anything AND does not itself introduce new concrete \
draft content ("what are common KPI frameworks for X?", "what's a reasonable way to weight \
A vs B?", "can you explain why you picked that threshold?") -> `respond_conversationally`. \
Use `web_search` first (see below — it is available on THIS and every turn, not just your \
very first) if you need current, real information you don't already have, then report it \
back conversationally.
- The user explicitly asks you to look something up ("can you look that up", "search for \
current X benchmarks") -> use `web_search`, then report what you found via \
`respond_conversationally` UNLESS they also told you what to do with the result (in which \
case fold it straight into `update_draft`).
- You are missing information you genuinely need before you can proceed at all -> \
`ask_clarification`. If you find yourself wanting to ask MORE THAN ONE question this turn \
(e.g. "do you want X or Y, and also are there any standards to follow, and also what kind \
of deliverables..."), do NOT write them out as a numbered/bulleted list anywhere (not in \
`ask_clarification`'s own `question`, which is a single string, and not in \
`update_draft`'s `assistant_message` or a `respond_conversationally` reply either) — every \
one of those is real UI the user can't click options on, just a text wall forcing a typed \
reply. Ask ONLY the single most important/blocking one via `ask_clarification` (with real \
chip options) this turn, and hold the rest for follow-up turns once this one is answered. \
`ask_clarification` is the ONLY tool whose question reaches the user as clickable option \
chips instead of prose — any question that matters enough for you to need the answer to \
belongs there, one at a time, never bundled into another tool's text field. \
Never let a genuine back-and-forth discussion get flattened into a rigid \
ask_clarification chip-question or an unwanted draft mutation — `respond_conversationally` \
exists precisely so a real conversation can happen in between. But it is never a \
substitute for `update_draft` when you are describing something you've actually decided —  \
"I described it in words" does not count as "I saved it to the draft." Nor is it (or \
`update_draft`'s `assistant_message`) ever a substitute for `ask_clarification` when you \
actually need an answer — "I asked it in words" does not count as "I asked it as a \
question the user can answer."

If the user's latest message contains a direct question (for example "why is this scorecard \
needed?" or "what is it for?"), ALWAYS answer it explicitly in the message the user will see \
(`assistant_message` or `respond_conversationally`'s reply), alongside any draft change — never \
instead of answering and never silently ignored.

If the draft already has KPIs (e.g. merged in from the multi-agent research fan-out that \
ran before your first turn this session — organized into named CATEGORIES, each a Level-1 \
KPI with NO weight of its own, grouping its Level-2 children, each child already has a \
name, weight, and full 11-level guidelines, grounded in real research), your job THIS TURN \
is RECONCILIATION, not fresh generation: review the set for genuine quality/coverage gaps \
or true near-duplicates (within a category AND across categories), do a final sanity pass \
on the weights (every LEAF KPI across the WHOLE draft — regardless of which category it's \
nested under — must together sum to 100; categories themselves never have a weight, see \
"Fields still missing/incomplete" below), and present it to the user for confirmation/ \
adjustment via ask_clarification rather than inventing an entirely new KPI list from \
scratch. PRESERVE the existing category structure (each KPI's `level`/`parent_name`) \
exactly as merged unless the user explicitly asks you to regroup something — never flatten \
an already-categorized KPI back to a bare top-level item. Only propose ADDITIONAL new KPIs \
via update_draft if there is a real, identified coverage gap the research didn't touch, and \
when you do, nest each new KPI under the MOST FITTING existing category (`parent_name` = \
that category's exact name, `level=2`) rather than appending it flat — only introduce a \
genuinely new category (a new `level=1` KPI of its own, with NO weight, whose children's \
weights you then fold into the full leaf set so it still re-sums to 100 overall) if the gap \
truly doesn't belong under any category already present. Do not discard or rewrite an \
already-researched KPI's guidelines just to "improve" them unless the user specifically \
asked you to change that KPI. If the ONLY thing missing is a scalar field (name/purpose/\
domain/audience/target_score, NOT the KPIs themselves), send update_draft with JUST those \
field(s) in `patch` and OMIT `kpis` entirely — do not re-type the existing KPI list just to \
fill in an unrelated field; omitting `kpis` from `patch` leaves every already-merged KPI, \
and its category grouping, exactly as it is, automatically. Re-typing a large KPI list from \
memory risks silently truncating it (or its category structure), which would undo the \
whole point of the research merge.

EDITING AN EXISTING DRAFT: for any follow-up change to KPIs that already exist — change a weight \
("make X 8% and scale the others down"), rename, remove, add one or a few KPIs, move to another \
category, include/exclude from scoring, rewrite one KPI's rubric — call `edit_kpis` with a SMALL \
list of operations. The server applies them, rebalances every leaf weight to exactly 100 and \
writes any new KPI's guidelines itself, so your output stays tiny however big the scorecard is. \
Use `update_draft` with a full `kpis` list ONLY for a wholesale restructuring; NEVER retype a \
large KPI list (with or without guidelines) to change a few KPIs.

USER-SUPPLIED KPIs OVERRIDE ALL OF THE ABOVE. When the user has given you their own explicit \
list of KPIs (a "USER-SPECIFIED KPIs" block below says so for the session's first message; \
the same applies if the user pastes a KPI list in ANY later message), that list is \
AUTHORITATIVE: keep every KPI exactly as named (no renaming, merging, deduplicating, \
re-grouping or dropping — even ones that look redundant, and however many there are), keep \
any hierarchy, weights and guidelines the user gave, and only FILL GAPS: missing scalar \
fields, missing 0-10 guidelines, weights the user didn't give (leaf weights must still sum to \
100 — if the user's don't, scale them proportionally and SAY so), and a scoring formula if \
asked or clearly useful. Do not add KPIs the user didn't ask for. State your assumptions \
(filled weights, normalization, renamed duplicates) in `assistant_message`. When you send a \
user-pasted list through update_draft, also list its exact KPI names in \
`user_specified_kpis` so the server protects them from later accidental edits.

If the draft has NO KPIs yet (e.g. the research fan-out found nothing for this domain, or \
was skipped), prefer `update_draft` with your own best-guess proposal before you \
`ask_clarification` (LLM first, human second — propose candidate KPIs from what the user \
already told you, then ask about what's genuinely ambiguous or missing). Where it's \
genuinely useful for the domain, organize your proposal into named categories the same way \
(a Level-1 KPI per category with NO weight of its own, Level-2 KPIs nested under it via \
`parent_name` each carrying its own weight) rather than one flat list — a flat list is \
still fine for a domain simple enough that categorization wouldn't add real clarity.

When a `web_search` tool is available to you, use it to research real industry KPIs, \
published standards, and benchmark thresholds for the user's stated domain BEFORE \
proposing quantitative guideline thresholds via `update_draft` — the framework requires \
replacing vague adjectives ("fast", "good") with real measures, and a real published \
benchmark is far better than a plausible-sounding invented number. Weave what you find \
into both the qualitative guideline text and the quantitative_criteria you propose. \
`web_search` is offered on EVERY turn it's configured for, not only your first — feel \
free to search again later in the conversation if the user pivots domain, asks a new \
research question, or explicitly asks you to look something up (see above); it isn't \
limited to the very first message.

The user may also ask you to change HOW the final score is computed — e.g. "weight X more \
heavily than a plain average would" or "use the minimum of these two KPIs instead of \
averaging them". By default (no custom formula set) the score is the classic weighted \
average: sum of each KPI's (weight/100 x score). To change this, call the dedicated \
`update_scoring_formula` tool (never smuggle a formula into update_draft's patch) with an \
expression referencing KPI scores as kpi["Exact KPI Name"] (supports + - * / ** and the \
functions min, max, avg, sqrt, abs) — it is validated server-side against the draft's \
current KPI names and rejected with a specific error if invalid, so retry with a \
corrected formula if that happens. Pass `formula: null` to clear a custom formula and \
revert to the default weighted average.
{scoring_formula_context}
Current draft (JSON): {draft_json}
Fields still missing/incomplete: {missing_fields}
{research_context}{user_spec_context}"""


class ChatTurn(TypedDict):
    role: str  # "user" | "assistant" | "tool"
    content: str


def _append(existing: list[Any], new: list[Any]) -> list[Any]:
    return [*existing, *new]


class BuilderState(TypedDict):
    session_id: str
    messages: Annotated[list[ChatTurn], _append]
    draft: dict[str, Any]
    pending_tool: dict[str, Any] | None
    pending_question: dict[str, Any] | None
    last_patch_error: str | None
    status: str  # "gathering" | "ready_to_confirm" | "confirmed"
    llm_turn_count: int  # consecutive propose_kpis calls since the last human turn
    # Leaf weights as they were when the CURRENT human turn started (set on the turn's first propose_kpis
    # visit): the baseline of the server-built edit summary, so it shows real old -> new values even when
    # the model applied part of the change in an earlier visit of the same turn.
    turn_base_weights: dict[str, Any] | None
    # Reuse-suggestion wiring (see check_similarity/suggest_similar below): guards the
    # similarity search to run exactly once per session (on the initial prompt), and
    # holds any qualifying matches between check_similarity and suggest_similar.
    similarity_checked: bool
    similar_suggestions: list[dict[str, Any]] | None
    # Multi-agent research fan-out wiring (see research_kpis/`_run_research_agent` above):
    # guards the fan-out to run exactly once per session (mirrors similarity_checked's own
    # pattern), and holds the consolidated findings for every propose_kpis call for the
    # rest of the session to ground its KPI/threshold proposals in.
    research_done: bool
    research_findings: list[dict[str, Any]] | None
    # On-demand mid-conversation web_search wiring (see MAX_WEB_SEARCH_CALLS_PER_SESSION
    # above and RESPOND_CONVERSATIONALLY_TOOL): total number of web_search calls
    # propose_kpis's own ad hoc loop has made across the WHOLE session so far (every human
    # turn, not just one node visit) — checkpointed like every other field here, so the
    # budget survives a restart/resume exactly as robustly as the rest of this state.
    web_search_calls_used: int
    # User-specified KPI mode (see the "User-specified KPI mode" section above and
    # `_user_spec_state`): None for an open-ended session; otherwise {mode, pinned:
    # [{name, parent_name, level}], notes, allow_additional, ...}. `pinned` is the set of KPI
    # names/hierarchy `update_draft` refuses to let a patch drop/rename/re-parent.
    user_spec: dict[str, Any] | None
    # How many user messages `research_kpis`/`ingest_user_kpis` have already examined for a
    # user-supplied KPI list (None = unknown/legacy checkpoint) — see `ingest_user_kpis`.
    user_msgs_checked: int | None
    # A requested KPI count and how the research step delivered it: None when none was asked for,
    # else {requested, delivered (None until research ran), note (shortfall/prune explanation)} —
    # see `_format_kpi_target_for_prompt`.
    kpi_target: dict[str, Any] | None


def initial_state(session_id: str, first_user_message: str) -> BuilderState:
    return BuilderState(
        session_id=session_id,
        messages=[{"role": "user", "content": first_user_message}],
        draft=ScorecardDraft().model_dump(mode="json"),
        pending_tool=None,
        pending_question=None,
        last_patch_error=None,
        status="gathering",
        llm_turn_count=0,
        similarity_checked=False,
        similar_suggestions=None,
        research_done=False,
        research_findings=None,
        web_search_calls_used=0,
        user_spec=None,
        user_msgs_checked=None,
        kpi_target=None,
    )


# --- Nodes ------------------------------------------------------------------------------


def gather_info(state: BuilderState) -> dict[str, Any]:
    """Entry node — runs only at the start of a genuine new human turn (a brand-new
    session, or `send_message` appending a fresh message with no pending interrupt; the
    resume-from-interrupt path goes ask_clarification -> propose_kpis directly and never
    passes through here). Ensures the draft is present/valid, and resets the per-human-turn
    LLM call budget (see MAX_LLM_TURNS_PER_HUMAN_TURN)."""
    draft = state.get("draft") or ScorecardDraft().model_dump(mode="json")
    ScorecardDraft.model_validate(draft)  # fail fast on corrupt checkpoint state
    return {"draft": draft, "llm_turn_count": 0}


def _last_user_message(messages: list[ChatTurn]) -> str:
    for turn in reversed(messages):
        if turn["role"] == "user":
            return turn["content"]
    return ""


def _conversation_context_text(messages: list[ChatTurn], max_turns: int = 8) -> str:
    """Plain-text rendering of the most recent user/assistant turns, for `research_kpis`'s
    `decide_categories` call (see `_decide_categories`) — this is deliberately
    NOT the structured `draft`, since on a session's first turn the draft is still entirely
    empty and the user's own words are the only real signal for the domain. Internal "tool"
    role notes (validation-error bookkeeping) are skipped as noise for this purpose."""
    relevant = [t for t in messages if t["role"] in ("user", "assistant")][-max_turns:]
    return "\n".join(f"{turn['role'].capitalize()}: {turn['content']}" for turn in relevant)


async def _session_owner_id(db: Any, thread_id: Any) -> Any:
    """The owning user of the chat session whose LangGraph thread id is `thread_id` (None if unknown)."""
    import uuid as _uuid

    from sqlalchemy import select as _select

    from app.models.chat_session import ChatSession

    try:
        sid = _uuid.UUID(str(thread_id))
    except (ValueError, TypeError):
        return None
    return (await db.execute(_select(ChatSession.user_id).where(ChatSession.id == sid))).scalar_one_or_none()


async def check_similarity(state: BuilderState, config: RunnableConfig) -> dict[str, Any]:
    """Reuse-suggestion wiring (fixes the gap where `find_similar_scorecards` was only
    reachable via the standalone `POST /scorecards/suggest-similar` endpoint): runs once,
    on the session's initial prompt/purpose text (guarded by `similarity_checked`),
    embeds it and searches `scorecard_embeddings` for an existing scorecard worth
    reusing (see `app/ai/similarity.py`) — "next time a similar query comes in, suggest
    existing stuff... before generating from scratch" per the plan. Qualifying matches
    (>= SIMILARITY_THRESHOLD) route to `suggest_similar` (below), which pauses the graph
    *before* `propose_kpis` ever runs, so the frontend can offer Use as-is / Adapt /
    Start fresh before any LLM generation happens. A no-op when no `db_session` is wired
    into `config` (e.g. this module's own unit tests, which exercise LLM-turn logic in
    isolation against a fake Bedrock client with no DB — see
    tests/test_scorecard_builder.py) or when nothing qualifies."""
    if state.get("similarity_checked"):
        return {}

    configurable = config.get("configurable", {})
    db = configurable.get("db_session")
    bedrock = configurable.get("bedrock_client")
    query_text = _last_user_message(state["messages"])

    if db is None or bedrock is None or not query_text.strip():
        return {"similarity_checked": True}

    try:
        # Only the session owner's OWN scorecards may be suggested (never another user's names / purposes). The
        # owner comes from the chat session row, so every caller (request or background worker) is covered;
        # an unknown session fails closed.
        owner_id = await _session_owner_id(db, configurable.get("thread_id"))
        if owner_id is None:
            return {"similarity_checked": True}
        results = await find_similar_scorecards(db, bedrock, query_text, top_n=3, owner_id=owner_id)
    except Exception:
        logger.warning(
            "check_similarity: similarity search failed; proceeding without suggestions.",
            exc_info=True,
        )
        return {"similarity_checked": True}

    if not results:
        return {"similarity_checked": True}

    suggestions = [
        {
            "scorecard_id": str(r.scorecard_id),
            "scorecard_version_id": str(r.scorecard_version_id),
            "name": r.name,
            "domain": r.domain,
            "similarity": r.similarity,
            "purpose_statement": r.purpose_statement,
        }
        for r in results
    ]
    return {"similarity_checked": True, "similar_suggestions": suggestions}


def _route_after_similarity(state: BuilderState) -> str:
    return "suggest_similar" if state.get("similar_suggestions") else "research_kpis"


def suggest_similar(state: BuilderState) -> dict[str, Any]:
    """Pauses the graph (mirrors `ask_clarification`'s interrupt pattern, see below) to
    offer the matches `check_similarity` found. Resumes on whatever the user answers —
    the frontend sends a normal follow-up chat message either way for Adapt/Start fresh
    (`Command(resume=...)`, handled identically to resuming from `ask_clarification` —
    see `send_message`); "Use as-is" is handled entirely client-side (navigates straight
    to the existing scorecard) and simply never resumes this pause, which is fine —
    indistinguishable from a user never answering an `ask_clarification` question."""
    payload = {"kind": "similar_suggestions", "suggestions": state.get("similar_suggestions") or []}
    answer = interrupt(payload)
    return {
        "similar_suggestions": None,
        "messages": [{"role": "user", "content": str(answer)}],
    }


# research_kpis (the multi-agent research fan-out master/orchestrator node), and the ONE
# `_run_research_agent` worker definition it invokes concurrently, live above — right after
# MAX_WEB_SEARCH_CALLS_PER_PROPOSE, since both reference WEB_SEARCH_TOOL/ResearchFinding and
# are logically part of the same "research tooling" section as that constant.


def _messages_to_converse(messages: list[ChatTurn]) -> list[dict[str, Any]]:
    converse_messages: list[dict[str, Any]] = []
    for turn in messages:
        role = "assistant" if turn["role"] in ("assistant", "tool") else "user"
        # Converse alternates user/assistant; a "tool" turn (our own validation-error
        # notes) is folded in as an assistant-authored note rather than a real toolResult
        # block, since we are not replaying the original toolUseId round-trip here — the
        # content is still visible to the model on the next turn either way.
        text = turn["content"]
        if turn["role"] == "tool" and turn.get("kind") != "edit_summary":
            # (The server's own edit summary is shown to the model as plain assistant text: a "[system note]"
            # prefix there made the model imitate it in its next reply.)
            text = f"[system note] {text}"
        if converse_messages and converse_messages[-1]["role"] == role:
            converse_messages[-1]["content"].append({"text": text})
        else:
            converse_messages.append({"role": role, "content": [{"text": text}]})
    return converse_messages


def _format_search_results(query: str, results: list[SearchResult]) -> str:
    if not results:
        return f"[web_search results] query={query!r}: no results (search failed or returned nothing)."
    lines = [f"[web_search results] query={query!r}, {len(results)} result(s):"]
    for r in results:
        date_suffix = f" [{r.published_date}]" if r.published_date else ""
        lines.append(f"- {r.title} ({r.url}){date_suffix}: {r.snippet}")
    return "\n".join(lines)


def _final_answer_gate_text(tool_name: str | None, tool_input: dict[str, Any], assistant_note: str) -> str:
    """Renders propose_kpis's decision for quality-gate checkpoint 3's `answer` (see that
    function). For `ask_clarification`/`respond_conversationally`, `assistant_note` IS
    already the full user-facing text — nothing more to add. For `update_draft`/
    `update_scoring_formula`, `assistant_note` alone ("Updating the draft.") says nothing
    about WHAT changed, so the actual patch/formula content is included too — that's what
    genuinely represents "the answer" being rated. Patch JSON is capped so an unusually
    large KPI patch doesn't blow up the Jev request."""
    if tool_name == "update_draft":
        patch_json = json.dumps(tool_input.get("patch") or {})[:4000]
        confirmed = bool(tool_input.get("confirmed"))
        return f"[update_draft] confirmed={confirmed}\nMessage shown to the user: {assistant_note}\nPatch: {patch_json}"
    if tool_name == "update_scoring_formula":
        return f"[update_scoring_formula] {assistant_note}\nFormula: {tool_input.get('formula')!r}"
    # ask_clarification / respond_conversationally: assistant_note already is the complete
    # user-facing text (the question, or the conversational reply) — nothing to add.
    return assistant_note


def _prompt_draft(draft: ScorecardDraft) -> dict[str, Any]:
    """The draft as shown to the model. For a big scorecard (>= 12 KPIs) the 11-level rubrics are
    summarised ("<11 levels>") — thousands of input tokens the model does not need for edits, and
    it cannot retype what it cannot see (a full-`kpis` patch keeps existing rubrics by name anyway)."""
    dumped = draft.model_dump(mode="json")
    if len(draft.kpis) >= 12:
        dumped["kpis"] = [
            {**k, "guidelines": f"<{len(k['guidelines'])} levels, preserved automatically>" if k["guidelines"] else {}}
            for k in dumped["kpis"]
        ]
    return dumped


_MUTATING_TOOLS = frozenset({"edit_kpis", "update_draft", "update_scoring_formula"})
_EDIT_VERBS = re.compile(
    r"\b(make|set|change|rename|remove|delete|drop|add|increase|decrease|raise|lower|reduce|scale|rebalance|"
    r"move|swap|exclude|include|bump|update|adjust)\b",
    re.IGNORECASE,
)
_WH_START = re.compile(r"^\s*(why|what|how|which|when|where|who|is|are|do|does|did)\b", re.IGNORECASE)
_PROMISE = re.compile(
    r"\b(let me (?:apply|do|make|update|change|set|edit)|i['\u2019]?ll (?:make|apply|update|change|set|do|edit)|"
    r"i will (?:make|apply|update|change|set|edit)|(?:applying|making) (?:this|that|the) (?:change|edit|update)|"
    r"let me do that|going to (?:apply|make|update|change))\b",
    re.IGNORECASE,
)


def _has_edit_intent(text: str, kpis: list[KpiDraft]) -> bool:
    """A clear edit instruction about the EXISTING draft: an edit verb plus a draft KPI name, a
    'scale/rebalance the others' phrase or a numeric percentage — and not a wh-question."""
    if not kpis or not text or _WH_START.search(text) or not _EDIT_VERBS.search(text):
        return False
    padded = f" {_normalize_kpi_name_for_dedup(text)} "
    names_hit = any(
        (norm := _normalize_kpi_name_for_dedup(k.name)) and f" {norm} " in padded for k in kpis
    )
    return names_hit or bool(_OTHERS_PHRASE.search(text)) or bool(re.search(r"\d+(?:\.\d+)?\s*%", text))


def _match_kpi_name(fragment: str, names: list[str]) -> str | None:
    norm = _normalize_kpi_name_for_dedup(fragment)
    if not norm:
        return None
    exact = [n for n in names if _normalize_kpi_name_for_dedup(n) == norm]
    if len(exact) == 1:
        return exact[0]
    import difflib

    scored = sorted(
        ((difflib.SequenceMatcher(None, norm, _normalize_kpi_name_for_dedup(n)).ratio(), n) for n in names),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1]
    return None


def _parse_simple_edit(text: str, draft: ScorecardDraft) -> list[dict[str, Any]]:
    """Deterministic fallback parser for the most common single edits, used only when the model failed to
    call a tool twice: 'make/set X (to) N%' (+ implicit proportional rescale of the others),
    'rename X to Y', 'remove/delete X'. Returns [] when the message is not one of those unambiguously."""
    parents = {k.parent_name for k in draft.kpis if k.parent_name}
    names = [k.name for k in draft.kpis]
    leaves = [k.name for k in draft.kpis if k.name not in parents]
    m = re.search(
        r"\b(?:make|set|change|update|bump|raise|lower|increase|decrease|reduce)\s+(?:the\s+)?(?:weight\s+of\s+)?"
        r"(.+?)\s*(?:\bweight\b)?\s*(?:\bto\b|\bat\b|=)?\s*(\d+(?:\.\d+)?)\s*%",
        text, re.IGNORECASE,
    )
    if m:
        target = _match_kpi_name(m.group(1), leaves)
        if target:
            return [{"op": "set_weight", "name": target, "weight": float(m.group(2)), "rebalance": "proportional"}]
    m = re.search(r"\brename\s+(.+?)\s+(?:to|as)\s+(.+?)(?:[.;]|$)", text, re.IGNORECASE)
    if m:
        target = _match_kpi_name(m.group(1), names)
        new_name = m.group(2).strip().strip('"\'')
        if target and new_name:
            return [{"op": "rename", "name": target, "new_name": new_name}]
    m = re.search(r"\b(?:remove|delete|drop)\s+(?:the\s+)?(.+?)(?:\s+kpi)?(?:[.;,]|\band\b|$)", text, re.IGNORECASE)
    if m:
        target = _match_kpi_name(m.group(1), names)
        if target:
            return [{"op": "remove", "name": target}]
    return []


FIRST_TURN_FAST_PATH = True  # tests that exercise the model visit of a prepared draft switch this off
FIRST_TURN_OPTIONS = [
    "Save it as-is", "Adjust weights", "Adjust thresholds/benchmarks", "Adjust a specific KPI's guidelines",
]
FIRST_TURN_QUESTION = "Would you like to save this scorecard as it is, or adjust anything first?"


SUMMARY_MAX_CHARS = 1200


def _cap_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Server-built chat summaries stay short: cut at a line boundary and say so."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0].rstrip()
    return cut + "\n- (more detail is in the scorecard)"


def _first_turn_summary(draft: ScorecardDraft, user_spec: dict[str, Any]) -> str:
    """Compact server-built summary of what preparation filled in (no model call, no per-KPI lists): header
    fields, a weighting line with the top/bottom weights from the SAVED draft, and the number of KPIs that
    need attention. Per-KPI rationales are not dumped into the chat."""
    parents = {k.parent_name for k in draft.kpis if k.parent_name}
    leaves = [k for k in draft.kpis if k.name not in parents]
    lines = [f"Built from your {len(leaves)} KPIs (names kept exactly as you gave them)."]
    filled = [
        label for label, value in (
            ("name", draft.name), ("purpose", draft.purpose), ("domain", draft.domain),
            ("audience", draft.audience), ("target score", draft.target_score),
        ) if value not in (None, "")
    ]
    if filled:
        lines.append("- Filled in: " + ", ".join(filled) + ".")
    if draft.name:
        target = f" (target {draft.target_score:g}/10)" if draft.target_score else ""
        lines.append(f"- Scorecard: {draft.name}{target}")
    weighted = sorted((k for k in leaves if k.weight is not None), key=lambda k: -float(k.weight or 0))
    user_weights = sum(1 for p in (user_spec.get("pinned") or []) if p.get("weight") is not None)
    if weighted:
        origin = (
            "none were given, so I set risk/impact-based relative weights"
            if user_weights == 0
            else f"you gave {user_weights}; I set the rest by risk/impact-based relative importance"
        )
        top = ", ".join(f"{k.name} ({float(k.weight):.1f}%)" for k in weighted[:5])
        line = f"- Weights: {origin} (total 100%). Highest: {top}."
        if len(weighted) > 8:
            low = ", ".join(f"{k.name} ({float(k.weight):.1f}%)" for k in weighted[-3:])
            line += f" Lowest: {low}."
        lines.append(line)
    lines.append("- Every KPI has a full 0-10 guideline.")
    fallback = [
        k.name for k in leaves
        if any("Auto-generated fallback" in g.qualitative_text for g in k.guidelines.values())
    ]
    if fallback:
        lines.append(
            f"- {len(fallback)} KPI(s) only have generic placeholder rubrics to review: "
            + ", ".join(fallback[:3]) + (f" and {len(fallback) - 3} more" if len(fallback) > 3 else "") + "."
        )
    return _cap_summary("\n".join(lines))


def _forced_pause_summary(draft: ScorecardDraft, state: BuilderState) -> str:
    """Deterministic status text for the forced pause: counts, what changed this turn, remaining gaps."""
    parents = {k.parent_name for k in draft.kpis if k.parent_name}
    leaves = [k for k in draft.kpis if k.name not in parents]
    lines = [f"Here is where the scorecard stands: {len(leaves)} KPIs across {len(parents)} categor"
             f"{'y' if len(parents) == 1 else 'ies'}."]
    base = state.get("turn_base_weights") or {}
    if base:
        now = {k.name: k.weight for k in draft.kpis}
        added = [n for n in now if n not in base]
        removed = [n for n in base if n not in now]
        changed = [n for n in now if n in base and base[n] != now[n]]
        if added or removed or changed:
            lines.append(
                f"- Changed this turn: {len(changed)} weight(s), {len(added)} KPI(s) added, {len(removed)} removed."
            )
    missing = draft.missing_fields()
    if missing:
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        lines.append("- Still missing: " + ", ".join(missing[:5]) + more)
    fallback = [
        k.name for k in leaves if any("Auto-generated fallback" in g.qualitative_text for g in k.guidelines.values())
    ]
    if fallback:
        lines.append("- Generic placeholder rubrics to review: " + ", ".join(fallback[:5]))
    return _cap_summary("\n".join(lines))


async def propose_kpis(state: BuilderState, config: RunnableConfig) -> dict[str, Any]:
    draft = ScorecardDraft.model_validate(state["draft"])
    turn_count = state.get("llm_turn_count", 0)
    session_id: str = state["session_id"]
    turn_started_at: datetime | None = (config.get("configurable") or {}).get("turn_started_at")

    if turn_count >= MAX_LLM_TURNS_PER_HUMAN_TURN:
        # Safety cutoff (see MAX_LLM_TURNS_PER_HUMAN_TURN): force a pause instead of
        # calling the model again, so a model that never calls ask_clarification/confirm
        # can't loop this node forever within one human turn.
        # Last-resort guard only (a successful edit / complete draft already ends the turn). The visible text
        # is built from the REAL draft state, never a generic "let's pause" line; the single question lives
        # in the card (see `_without_card_question`).
        logger.warning("propose_kpis hit MAX_LLM_TURNS_PER_HUMAN_TURN; forcing a pause.")
        summary = _forced_pause_summary(draft, state)
        pending_tool = {
            "name": "ask_clarification",
            "input": {
                "question": FIRST_TURN_QUESTION,
                "options": FIRST_TURN_OPTIONS,
                "missing_fields": draft.missing_fields(),
            },
        }
        return {
            "pending_tool": pending_tool,
            "messages": [
                {"role": "assistant", "content": summary},
                {"role": "assistant", "content": FIRST_TURN_QUESTION},
            ],
            "llm_turn_count": turn_count + 1,
        }

    configurable = config.get("configurable", {})
    bedrock: BedrockClientProtocol = configurable["bedrock_client"]
    model_id: str | None = configurable.get("chat_model_id")
    web_search_client: WebSearchClientProtocol | None = configurable.get("web_search_client")
    jev_client: JevClientProtocol | None = configurable.get("jev_client")

    # Safety net: a leaf with fewer than 11 guideline levels (a model patch, a refined legacy
    # scorecard, ...) is repaired in small compact chunks BEFORE the model sees the draft, so the
    # draft can never be confirmed/saved incomplete. Persisted via the node's return value.
    repaired_draft: dict[str, Any] | None = None
    current_kpis = [k.model_dump(mode="json") for k in draft.kpis]
    if _incomplete_leaves(current_kpis):
        fixed_kpis, fb_names = await _repair_incomplete_leaves(
            current_kpis, bedrock, model_id, _conversation_context_text(state["messages"]),
            session_id=session_id, turn_started_at=turn_started_at,
        )
        if fb_names:
            logger.warning("propose_kpis: %d KPI(s) got the marked fallback rubric: %s", len(fb_names), fb_names)
        repaired_draft = {**draft.model_dump(mode="json"), "kpis": fixed_kpis}
        draft = ScorecardDraft.model_validate(repaired_draft)

    # First turn of a user-specified / hybrid session whose draft preparation already completed (KPIs,
    # rubrics, weights, header): nothing is left for the model to decide, so skip the model visit entirely
    # (it used to retype the whole KPI list: 2-3 unpredictable minutes). The visible text is the side-question
    # answer (already in the transcript), a server-built summary and ONE closing question.
    spec_state = state.get("user_spec") or {}
    first_message_now = sum(1 for t in state["messages"] if t["role"] == "user") <= 1
    if (
        FIRST_TURN_FAST_PATH
        and turn_count == 0
        and first_message_now
        and spec_state.get("pinned")
        and draft.is_complete()
        and not (spec_state.get("scoring_formula_hint") and not draft.scoring_formula)
        and not (
            _real_questions(_last_user_message(state["messages"])) and _visible_turn_notes(state["messages"]) is None
        )
    ):
        summary = _first_turn_summary(draft, spec_state)
        await emit_turn_event(
            session_id, turn_started_at, "master", "completed", f"Asking: {FIRST_TURN_QUESTION}"
        )
        result_update_fast: dict[str, Any] = {
            "pending_tool": {
                "name": "ask_clarification",
                "input": {"question": FIRST_TURN_QUESTION, "options": FIRST_TURN_OPTIONS, "missing_fields": []},
            },
            "messages": [
                {"role": "assistant", "content": summary},
                {"role": "assistant", "content": FIRST_TURN_QUESTION},
            ],
            "llm_turn_count": turn_count + 1,
            "web_search_calls_used": state.get("web_search_calls_used", 0),
            "turn_base_weights": {k.name: k.weight for k in draft.kpis},
        }
        if repaired_draft is not None:
            result_update_fast["draft"] = repaired_draft
        return result_update_fast

    # Follow-up turns: a side question in the user's message is answered by a dedicated small call that
    # runs concurrently with this visit (the first turn does it in research_kpis). Edit instructions
    # phrased as questions are skipped; the answer is dropped if the model's own note already gives it.
    side_answer_task: asyncio.Task[str | None] | None = None
    if turn_count == 0 and sum(1 for t in state["messages"] if t["role"] == "user") > 1:
        follow_questions = _real_questions(_last_user_message(state["messages"]))
        if follow_questions:
            side_answer_task = asyncio.create_task(
                _answer_side_questions(
                    bedrock, model_id, _conversation_context_text(state["messages"]), follow_questions
                )
            )

    # Local working copy of the turn's conversation, extended in-place across any
    # web_search iterations below (the model's own tool call + the results fed back to
    # it) so it keeps full context within this node visit. Only the NEW entries appended
    # here (plus the final assistant_note) are returned at the end — state["messages"]
    # uses an append-only reducer (see _append), so returning the whole list would
    # duplicate everything already checkpointed.
    local_messages: list[ChatTurn] = list(state["messages"])
    search_calls_made = 0
    truncation_retries = 0
    # Session-level budget already spent in EARLIER human turns (see
    # MAX_WEB_SEARCH_CALLS_PER_SESSION's own docstring) — `search_calls_made` above only
    # counts calls made within THIS node visit; the two are summed below wherever the
    # session cap is checked, and the running total is persisted back to state at the end.
    web_search_calls_used_before = state.get("web_search_calls_used", 0)

    # Consolidated context from the (at-most-once-per-session) research fan-out — see
    # research_kpis/_run_research_agent above. Persists in state across every propose_kpis
    # visit for the rest of the session, so a later ask_clarification/update_draft loop
    # still sees the same grounding without re-running the fan-out.
    research_findings = state.get("research_findings") or []
    research_context = _format_research_findings_for_prompt(research_findings)
    # User-specified KPI mode (see the section above `_web_search_usable`): empty for an
    # open-ended session, otherwise the authoritative pinned-KPI rules for the whole session.
    user_spec_context = _format_user_spec_for_prompt(state.get("user_spec"))
    kpi_target_context = _format_kpi_target_for_prompt(state.get("kpi_target"))
    if kpi_target_context:
        user_spec_context = f"{user_spec_context}\n{kpi_target_context}" if user_spec_context else kpi_target_context

    # User-specified/hybrid session: the KPI set, guidelines and weights are already prepared, so this
    # step only completes the scalar fields, answers the user's question and presents the draft. The
    # Jev gate (it can never pass a draft the user's own KPIs shaped, and each retry re-searched) is
    # skipped, and on the session's FIRST user message web_search is not offered at all.
    user_mode = bool((state.get("user_spec") or {}).get("pinned"))
    first_user_message = sum(1 for t in state["messages"] if t["role"] == "user") <= 1
    search_blocked = user_mode and first_user_message
    # Edit instruction on an existing draft that has NOT been applied yet this turn: the model may only pick
    # a mutating tool (it once PROMISED the edit in a conversational reply and changed nothing).
    base_weights = state.get("turn_base_weights") or {}
    change_applied = turn_count > 0 and bool(base_weights) and {k.name: k.weight for k in draft.kpis} != base_weights
    last_user_text = _last_user_message(state["messages"])
    edit_intent = (
        not first_user_message and not change_applied and _has_edit_intent(last_user_text, draft.kpis)
    )
    edit_nudges = 0
    scalar_nag_retries = 0
    propose_started = time.monotonic()

    await emit_turn_event(
        session_id, turn_started_at, "master", "proposing",
        "Proposing KPIs and guidelines"
        + (", grounded in the research findings above" if research_context else "")
        + "…",
    )

    # Quality-gate checkpoint 3 (see the module comment above MAX_QUALITY_GATE_RETRIES)
    # wraps the WHOLE decision loop below in a bounded outer retry: after the model
    # produces its pending_tool/assistant_note for this node visit, Jev rates how well it
    # serves the user's actual request; below threshold, feedback is appended to
    # local_messages (mirroring update_draft's own "tool"-role rejection-note pattern) and
    # the decision loop runs again. best_* track the best-scoring attempt across retries
    # so a never-passing gate still proceeds with its best attempt rather than hanging.
    pending_tool: dict[str, Any] = {}
    assistant_note = ""
    best_pending_tool: dict[str, Any] | None = None
    best_assistant_note: str | None = None
    best_gate_score = -1.0
    revision_critique: str | None = None
    # Tracks the PREVIOUS attempt's rendered decision text, so a second-failure critique can
    # see what actually changed in response to the first critique (see
    # `_generate_quality_gate_critique`'s own docstring).
    previous_gate_answer: str | None = None

    for gate_attempt in range(MAX_QUALITY_GATE_RETRIES + 1):
        result = None
        while True:
            session_budget_available = (
                web_search_calls_used_before + search_calls_made < MAX_WEB_SEARCH_CALLS_PER_SESSION
            )
            search_budget_available = (
                web_search_client is not None
                and not search_blocked
                and search_calls_made < MAX_WEB_SEARCH_CALLS_PER_PROPOSE
                and session_budget_available
            )
            tools = _TOOLS_WITH_SEARCH if search_budget_available else _TOOLS
            if edit_intent:
                tools = [
                    t for t in _TOOLS
                    if t.name in ("edit_kpis", "update_draft", "update_scoring_formula", "ask_clarification")
                ]
            if research_findings and first_user_message and not user_mode and draft.is_complete():
                # Research just produced a complete draft: this step only PRESENTS it. update_draft is
                # not offered (re-typing the KPI list is how KPIs got silently dropped); edits go
                # through edit_kpis, a save request comes on a later turn.
                tools = [t for t in tools if t.name != "update_draft"]

            scoring_formula_context = (
                f'\nCurrent custom scoring_formula: {draft.scoring_formula!r} '
                "(non-null means the default weighted average is currently OVERRIDDEN).\n"
                if draft.scoring_formula
                else "\nCurrent custom scoring_formula: none set (using the default weighted average).\n"
            )
            system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
                draft_json=json.dumps(_prompt_draft(draft)),
                missing_fields=draft.missing_fields() or "(none — draft looks complete)",
                research_context=f"\n{research_context}\n" if research_context else "",
                user_spec_context=f"\n{user_spec_context}\n" if user_spec_context else "",
                scoring_formula_context=scoring_formula_context,
            )
            if web_search_client is not None and not search_budget_available:
                # Forced-closure step (see MAX_WEB_SEARCH_CALLS_PER_PROPOSE /
                # MAX_WEB_SEARCH_CALLS_PER_SESSION): web_search is no longer offered in `tools`
                # at all, so the model literally cannot call it again — force_tool_use means it
                # must pick one of the remaining tools. This instruction just makes the "why"
                # explicit to the model too.
                budget_reason = (
                    "your KPIs' research was already done before this step"
                    if search_blocked
                    else "you've used this session's whole web research budget"
                    if not session_budget_available
                    else "you have used your web research budget for this turn"
                )
                system_prompt += (
                    f"\n\nweb_search is no longer available ({budget_reason}). You now have "
                    "enough information to proceed — respond with update_draft, "
                    "ask_clarification, or respond_conversationally now."
                )
            if draft.is_complete():
                # Convergence fix (found via a real live run: a fully-grounded, already-
                # complete draft caused the model to burn its remaining MAX_LLM_TURNS_PER_
                # HUMAN_TURN budget calling update_draft again and again with an empty or
                # byte-for-byte-unchanged patch — never actually asking the user whether to
                # save, and never reaching confirmed=True within budget. `missing_fields`
                # already told the model the draft looks complete, but that alone wasn't a
                # strong enough signal to stop it re-sending no-op patches — this makes the
                # "what to do about it" explicit instead of just the state.
                system_prompt += (
                    "\n\nThe draft is currently COMPLETE (see 'Fields still missing/incomplete' "
                    "above). Do NOT call update_draft again with an empty patch or a patch that "
                    "doesn't actually change anything — that wastes a turn. Exactly one of the "
                    "following now: (a) the user's most recent message already told you to save "
                    "it — call update_draft with confirmed=true right now (patch may be `{}` if "
                    "nothing is changing); (b) you have a genuine, real change to make — make it "
                    "via update_draft; (c) neither of those — call ask_clarification to ask "
                    "whether they'd like to save it as-is or change something, instead of "
                    "calling update_draft again."
                )

            result = await asyncio.to_thread(
                bedrock.converse,
                messages=_messages_to_converse(local_messages),
                system=system_prompt,
                tools=tools,
                force_tool_use=True,
                model_id=model_id,
            )

            if result.truncated:
                # The model hit its output-token limit mid tool call, so the tool input is
                # incomplete (typically a huge `update_draft` KPI list). Never trust it: tell the
                # model to send far less, retry a bounded number of times, then fall back to a
                # plain-text clarification (below) instead of applying a half-written patch.
                truncation_retries += 1
                logger.warning("propose_kpis: model output truncated (retry %d).", truncation_retries)
                if truncation_retries <= MAX_TRUNCATION_RETRIES:
                    local_messages.append(
                        {
                            "role": "tool",
                            "content": (
                                "[system] Your previous tool call was CUT OFF (output limit reached) and was "
                                "discarded. Make a much smaller call: omit `kpis` from `patch` unless it is "
                                "essential (the existing KPI list is preserved when omitted), keep text short."
                            ),
                        }
                    )
                    continue
                result = ConverseResult(
                    stop_reason="max_tokens",
                    text=(
                        "My last response was too long to finish, so nothing was changed. Could you tell me which "
                        "part to work on next (for example one category at a time)?"
                    ),
                )
                break

            if result.is_tool_use and result.tool_name == "web_search" and search_budget_available:
                query = str((result.tool_input or {}).get("query") or "").strip()
                search_calls_made += 1
                await emit_turn_event(session_id, turn_started_at, "master", "searching", f'Searching: "{query}"')
                logger.info(
                    "propose_kpis: model called web_search (%d/%d this turn) query=%r",
                    search_calls_made,
                    MAX_WEB_SEARCH_CALLS_PER_PROPOSE,
                    query,
                )
                try:
                    results = await web_search_client.search(query) if query else []
                except Exception:  # noqa: BLE001 — belt-and-suspenders; see web_search.py's
                    # own contract that .search() never raises. A failed search must never
                    # kill the chat turn, so even a surprise exception here is swallowed.
                    logger.warning("web_search call raised unexpectedly; treating as no results.", exc_info=True)
                    results = []
                await emit_turn_event(
                    session_id, turn_started_at, "master", "search_result",
                    f'Found {len(results)} result(s) for "{query}"',
                )
                logger.info(
                    "propose_kpis: web_search query=%r returned %d result(s)%s",
                    query,
                    len(results),
                    f"; first={results[0].title!r} ({results[0].url})" if results else "",
                )
                local_messages.append(
                    {"role": "assistant", "content": f"[called web_search] query={query!r}"}
                )
                local_messages.append({"role": "tool", "content": _format_search_results(query, results)})
                continue

            if (
                user_mode
                and result.is_tool_use
                and result.tool_name == "ask_clarification"
                and scalar_nag_retries < 1
                and any(f in ("name", "purpose", "domain", "target_score") for f in draft.missing_fields())
            ):
                # The user already gave their use case and KPIs: name/purpose/domain/audience/target
                # score are the assistant's to infer, never a question. One bounded nudge.
                scalar_nag_retries += 1
                local_messages.append(
                    {
                        "role": "tool",
                        "content": (
                            "[system] Do NOT ask the user for the scorecard name, purpose, domain, audience or "
                            "target score — infer them from what they already said (their use case and KPIs) and "
                            "call update_draft NOW with just those fields in `patch` (omit `kpis`). Answer any "
                            "question they asked in `assistant_message`."
                        ),
                    }
                )
                continue

            if edit_intent and result.is_tool_use and result.tool_name not in _MUTATING_TOOLS:
                note_input = result.tool_input or {}
                note_text = str(note_input.get("response") or note_input.get("question") or "")
                if result.tool_name == "respond_conversationally" or _PROMISE.search(note_text):
                    # Promise-without-action: never surface it. One nudge with an edit-only tool set, then the
                    # deterministic parser for the common simple edits.
                    if edit_nudges < 1:
                        edit_nudges += 1
                        logger.warning("propose_kpis: edit instruction answered without an edit; nudging once.")
                        local_messages.append(
                            {
                                "role": "tool",
                                "content": (
                                    "[system] The user asked for a change and you have NOT made it. Call `edit_kpis` "
                                    "NOW with the operation(s) (one set_weight op with rebalance 'proportional' for "
                                    "'make X N% and scale the others'). Do not reply conversationally."
                                ),
                            }
                        )
                        continue
                    fallback_ops = _parse_simple_edit(last_user_text, draft)
                    if fallback_ops:
                        logger.warning("propose_kpis: applying the edit deterministically: %s", fallback_ops)
                        result = ConverseResult(
                            stop_reason="tool_use", tool_name="edit_kpis",
                            tool_input={"ops": fallback_ops, "assistant_message": ""},
                        )
            break

        if result.is_tool_use:
            pending_tool = {"name": result.tool_name, "input": result.tool_input or {}}
            # The persisted/returned message content is what the frontend renders directly as
            # the assistant's chat bubble (see _to_turn_result -> ChatTurnRead.assistant_message
            # -> MessageBubble) — it must be real, user-facing text, never an internal
            # "[called tool_name] {raw json}" debug label. `ask_clarification`'s structured
            # question/options/missing_fields still flow separately via the interrupt payload
            # itself (see ask_clarification node) for ClarifyingQuestionCard to render; this is
            # just the plain-text echo of the same question shown in the transcript bubble
            # above that card. `respond_conversationally` is the tool this whole mechanism
            # exists for — its `response` field IS the user-facing message, verbatim.
            tool_input = {
                **(result.tool_input or {}),
                **{
                    k: _strip_system_prefix(v)
                    for k, v in (result.tool_input or {}).items()
                    if k in ("question", "response", "assistant_message") and isinstance(v, str)
                },
            }
            pending_tool = {"name": result.tool_name, "input": tool_input}
            if result.tool_name == "edit_kpis":
                # Guidelines for added / regenerated KPIs are written HERE by the compact-rubric
                # machinery (small parallel calls), so the main model's output stays O(ops).
                gen_items: list[dict[str, Any]] = []
                for op in tool_input.get("ops") or []:
                    if isinstance(op, dict) and op.get("op") in ("add", "set_guidelines") and op.get("name"):
                        parent = op.get("parent_name")
                        if op["op"] == "set_guidelines":
                            old = next((k for k in draft.kpis if k.name == op["name"]), None)
                            parent = old.parent_name if old is not None else None
                        gen_items.append(
                            {"name": str(op["name"]), "parent_name": parent, "guidance": str(op.get("rationale") or "")}
                        )
                if gen_items:
                    await emit_turn_event(
                        session_id, turn_started_at, "master", "guidelines",
                        f"Writing guidelines for {len(gen_items)} KPI(s)…",
                    )
                    generated, fb = await _fill_missing_guidelines(
                        gen_items, bedrock, model_id, _conversation_context_text(local_messages), None
                    )
                    if fb:
                        logger.warning("edit_kpis: %d KPI(s) got the marked fallback rubric: %s", len(fb), fb)
                    tool_input = {**tool_input, "_generated": generated}
                    pending_tool = {"name": result.tool_name, "input": tool_input}
            if result.tool_name == "ask_clarification":
                assistant_note = str(tool_input.get("question") or "").strip() or (
                    "Could you tell me more about what this scorecard should measure?"
                )
            elif result.tool_name == "respond_conversationally":
                assistant_note = str(tool_input.get("response") or "").strip() or (
                    "(The assistant didn't include a response — please try rephrasing.)"
                )
            elif result.tool_name == "update_scoring_formula":
                formula = tool_input.get("formula")
                assistant_note = str(tool_input.get("assistant_message") or "").strip() or (
                    f'Setting a custom scoring formula: {formula!r}'
                    if formula
                    else "Clearing the custom scoring formula — reverting to the default weighted average."
                )
            else:  # update_draft
                # `assistant_message` (required on the tool schema — see UPDATE_DRAFT_TOOL)
                # is the model's own real, descriptive explanation of what it just drafted/
                # changed. This used to be a hardcoded "Updating the draft." label with no
                # content — which gave the model a structural incentive to describe drafted
                # KPIs/fields via `respond_conversationally` instead (the only tool whose
                # output became a real chat message), leaving the actual draft unpopulated
                # even when the assistant's prose clearly described concrete content. Falling
                # back to the old generic text only if the model somehow omits it.
                assistant_note = str(tool_input.get("assistant_message") or "").strip() or (
                    "Confirming and saving the draft."
                    if tool_input.get("confirmed")
                    else "Updating the draft."
                )

            # --- Quality-gate checkpoint 3 (see the module comment above
            # MAX_QUALITY_GATE_RETRIES): rate how well THIS decision serves the user's
            # actual request — instruction=the recent conversation, answer=a text
            # rendering of whatever this decision actually is (not just assistant_note
            # alone for update_draft/update_scoring_formula, since "Updating the draft."
            # says nothing about WHAT changed — see _final_answer_gate_text).
            if user_mode or draft.is_complete() or result.tool_name == "edit_kpis":
                # No Jev gate / retry loop for user-specified sessions (see above), nor when the draft
                # is already complete and valid: this step then only presents/reconciles it, and Jev
                # scored such terse decisions 0.03-0.72 (it judges a JSON patch against the whole
                # conversation, not real quality) — two retries cost ~5 minutes for nothing.
                break
            gate_instruction = _conversation_context_text(local_messages) or "(nothing yet)"
            gate_answer = _final_answer_gate_text(result.tool_name, tool_input, assistant_note)
            gate = await _gate(
                jev_client, instruction=gate_instruction, answer=gate_answer, turn_started_at=turn_started_at
            )

            if gate.degraded:
                await emit_turn_event(
                    session_id, turn_started_at, "master", "quality_gate",
                    "Response quality check: quality check unavailable or skipped (time budget) — treated as passed "
                    "(graceful degradation).",
                )
                break  # Jev unreachable — gate passed by policy; this decision is final.

            assert gate.score is not None  # guaranteed whenever degraded=False
            if gate.score > best_gate_score:
                best_pending_tool, best_assistant_note, best_gate_score = pending_tool, assistant_note, gate.score

            if gate.passed:
                await emit_turn_event(
                    session_id, turn_started_at, "master", "quality_gate",
                    f"Response quality check scored {gate.score:.2f} (>= {QUALITY_GATE_THRESHOLD}) — "
                    + ("passed after revision." if gate_attempt > 0 else "passed."),
                )
                break

            if gate_attempt < MAX_QUALITY_GATE_RETRIES:
                await emit_turn_event(
                    session_id, turn_started_at, "master", "quality_gate_retry",
                    f"Quality check scored {gate.score:.2f} (below {QUALITY_GATE_THRESHOLD}) — "
                    "revising the response…",
                )
                # Advisor/critique step (see the module comment above
                # MAX_QUALITY_GATE_RETRIES): a concrete, specific critique of THIS decision
                # in place of the old generic "reconsider and improve" note — on the second
                # failure, also carries what the first critique suggested and what actually
                # changed, so the critique is refined rather than repeated.
                revision_critique = await _generate_quality_gate_critique(
                    bedrock, model_id,
                    task_context=gate_instruction,
                    produced_output=gate_answer,
                    gate_score=gate.score,
                    previous_critique=revision_critique,
                    previous_output=previous_gate_answer,
                )
                previous_gate_answer = gate_answer
                local_messages.append(
                    {
                        "role": "tool",
                        "content": (
                            f"[quality gate] Your previous response was:\n{gate_answer}\n\n{revision_critique}"
                        ),
                    }
                )
                continue

            # Bounded cap reached and still below threshold — proceed with the best-scoring
            # attempt seen across every gate_attempt (never hang, never silently drop it).
            pending_tool = best_pending_tool if best_pending_tool is not None else pending_tool
            assistant_note = best_assistant_note if best_assistant_note is not None else assistant_note
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate",
                f"Response quality check still below {QUALITY_GATE_THRESHOLD} after "
                f"{MAX_QUALITY_GATE_RETRIES} revision(s) (best score {best_gate_score:.2f}) — "
                "proceeding with the best attempt.",
            )
            break
        else:
            # Defensive fallback: force_tool_use was requested but the model still replied
            # in plain text (e.g. a provider that silently ignores toolChoice). Convert it
            # into a well-formed ask_clarification so the graph's invariant — every turn
            # resolves to exactly one of the two tool branches — always holds. NOT quality-
            # gated: this is already a synthesized fallback, not a genuine model answer to
            # critique, and retrying it would just call the same broken-toolChoice path again.
            fallback_question = result.text or "Could you tell me more about what this scorecard should measure?"
            pending_tool = {
                "name": "ask_clarification",
                "input": {
                    "question": fallback_question,
                    "options": [],
                    "missing_fields": draft.missing_fields(),
                },
            }
            assistant_note = fallback_question
            logger.warning(
                "Model replied without a tool call despite force_tool_use; "
                "synthesized a fallback ask_clarification."
            )
            break

    new_messages: list[ChatTurn] = local_messages[len(state["messages"]) :]
    if side_answer_task is not None:
        side_answer = await side_answer_task
        if side_answer and not _answers_already(assistant_note, side_answer):
            new_messages.append({"role": "assistant", "content": side_answer})
    if pending_tool.get("name") == "edit_kpis":
        assistant_note = ""  # the visible text is the server-built change summary (see update_draft)
    if assistant_note:
        new_messages.append({"role": "assistant", "content": assistant_note})

    if pending_tool.get("name") == "ask_clarification":
        completed_message = f"Asking: {pending_tool.get('input', {}).get('question', '')}"
    elif pending_tool.get("name") == "respond_conversationally":
        completed_message = "Responding conversationally — draft left unchanged."
    elif (pending_tool.get("input") or {}).get("confirmed"):
        completed_message = "Draft confirmed — saving the scorecard."
    else:
        completed_message = "Updated the draft."
    await _emit_phase(session_id, turn_started_at, "reconcile", propose_started)
    await emit_turn_event(session_id, turn_started_at, "master", "completed", completed_message)

    result_update: dict[str, Any] = {
        "pending_tool": pending_tool,
        "messages": new_messages,
        "llm_turn_count": turn_count + 1,
        "web_search_calls_used": web_search_calls_used_before + search_calls_made,
    }
    if repaired_draft is not None:
        result_update["draft"] = repaired_draft
    if turn_count == 0:
        result_update["turn_base_weights"] = {k.name: k.weight for k in draft.kpis}
    return result_update


def _route_after_propose(state: BuilderState) -> str:
    pending = state.get("pending_tool") or {}
    name = pending.get("name")
    if name == "ask_clarification":
        return "ask_clarification"
    if name == "update_scoring_formula":
        return "update_scoring_formula"
    if name == "respond_conversationally":
        return "respond_conversationally"
    return "update_draft"


def ask_clarification(state: BuilderState) -> dict[str, Any]:
    question_payload = (state.get("pending_tool") or {}).get("input", {})
    # Pauses the graph here; AsyncPostgresSaver has already checkpointed everything
    # returned by prior nodes, so a crash/restart before the human answers loses nothing.
    # Resuming (`Command(resume=answer_text)`) re-enters this node and `interrupt()`
    # returns `answer_text` directly.
    answer = interrupt(question_payload)
    return {
        "pending_tool": None,
        "pending_question": None,
        "messages": [{"role": "user", "content": str(answer)}],
        # The human just answered — give the model a fresh LLM-turn budget for whatever
        # comes next (see MAX_LLM_TURNS_PER_HUMAN_TURN).
        "llm_turn_count": 0,
    }


def respond_conversationally(state: BuilderState) -> dict[str, Any]:
    """Pauses the graph after a genuinely conversational, NON-mutating reply — the model
    answered a question / discussed options / reported research findings via
    `respond_conversationally` (see RESPOND_CONVERSATIONALLY_TOOL) without touching
    `draft.kpis`/`draft.scoring_formula`/any scalar field. Mirrors `ask_clarification`'s
    own `interrupt()` pattern exactly (same crash/restart safety — AsyncPostgresSaver has
    already checkpointed everything up to this point): the human turn ends here, waiting
    for the user's actual next message, and resuming re-enters this node with `answer`
    bound to whatever they said, handled identically to `ask_clarification`'s own resume
    path (a fresh `llm_turn_count` budget, the answer appended as a new user turn).

    The interrupt VALUE's `kind: "conversational_response"` marker (as opposed to
    `ask_clarification`'s bare `{question, options, missing_fields}` payload, or
    `suggest_similar`'s `kind: "similar_suggestions"`) is what `_to_turn_result` uses to
    map this turn to `status: "gathering"` with `question: None` — i.e. render as a plain
    assistant chat bubble, never `ClarifyingQuestionCard`'s chip UI (see that function and
    `ChatTurnRead.question` in app/schemas/chat.py)."""
    response_payload = (state.get("pending_tool") or {}).get("input", {})
    payload = {"kind": "conversational_response", "response": response_payload.get("response", "")}
    answer = interrupt(payload)
    return {
        "pending_tool": None,
        "pending_question": None,
        "messages": [{"role": "user", "content": str(answer)}],
        "llm_turn_count": 0,
    }


def _plausibly_contains_kpi_list(text: str) -> bool:
    return plausibly_contains_kpi_list(text)


def _rebalance_leaves(leaves: list[dict[str, Any]], fixed: set[str]) -> bool:
    """Makes the scored `leaves`' weights sum to exactly 100, in place, changing only the
    non-`fixed` (model-filled) ones whenever that is possible; returns True only if the fixed
    (user-supplied) weights had to be scaled too (no flexible leaf, or the fixed ones alone
    already reach 100) so the caller can tell the user."""
    if not leaves:
        return False
    flex = [leaf for leaf in leaves if leaf["name"] not in fixed]
    fixed_leaves = [leaf for leaf in leaves if leaf["name"] in fixed]
    fixed_sum = sum(float(leaf.get("weight") or 0.0) for leaf in fixed_leaves)
    flex_sum = sum(float(leaf.get("weight") or 0.0) for leaf in flex)
    scaled_fixed = False
    if flex and fixed_sum < 99.99:
        room = 100.0 - fixed_sum
        for leaf in flex:
            leaf["weight"] = (
                float(leaf.get("weight") or 0.0) * room / flex_sum if flex_sum > 0 else room / len(flex)
            )
    else:
        total = fixed_sum + flex_sum
        if total <= 0:
            for leaf in leaves:
                leaf["weight"] = 100.0 / len(leaves)
        elif abs(total - 100.0) > 0.01:
            for leaf in leaves:
                leaf["weight"] = float(leaf.get("weight") or 0.0) * 100.0 / total
        scaled_fixed = bool(fixed_leaves) and abs(total - 100.0) > 0.01
    for leaf in leaves:
        leaf["weight"] = round(float(leaf["weight"]), 2)
    drift = round(100.0 - sum(leaf["weight"] for leaf in leaves), 2)
    if abs(drift) >= 0.01:
        target = max(flex or leaves, key=lambda leaf: leaf["weight"])
        target["weight"] = round(target["weight"] + drift, 2)
    return scaled_fixed


def _merge_followup_into_draft(
    draft: ScorecardDraft, user_spec: dict[str, Any] | None, spec: UserSpec, pinned: PinnedKpis
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Folds a mid-conversation user KPI list into an EXISTING draft: returns `(kpis, pins,
    notes)`. Existing KPIs are untouched except (a) user-named overlaps get the weight/rubric
    the user just gave, (b) an existing leaf the new list nests KPIs under becomes a category
    (no weight/guidelines), and (c) leaf weights are rebalanced to 100 changing only
    model-filled weights where possible (`_rebalance_leaves`). User-supplied weights — the
    new ones and previously pinned ones — are frozen at their final values."""
    notes = list(pinned.notes)
    existing = [k.model_dump(mode="json") for k in draft.kpis]
    by_name = {k["name"]: k for k in existing}
    prev_pins = {p["name"]: dict(p) for p in (user_spec or {}).get("pinned") or []}
    user_w_new, user_g_new = _user_supplied_names(spec.kpis)

    for ov in spec.overlaps:
        node = by_name[ov["name"]]
        try:
            weight = float(ov["weight"]) if ov.get("weight") is not None else None
        except (TypeError, ValueError):
            weight = None
        if weight is not None and 0 <= weight <= 100:
            node["weight"] = weight
            user_w_new.add(node["name"])
        raw_rubric = ov.get("guidelines")
        if isinstance(raw_rubric, dict) and raw_rubric:
            try:
                parsed = KpiDraft.model_validate(
                    _normalize_kpi_guidelines({"name": node["name"], "guidelines": raw_rubric})
                )
                dumped = {k: v.model_dump(mode="json") for k, v in parsed.guidelines.items()}
                if _complete_guidelines(dumped):
                    node["guidelines"] = dumped
                    user_g_new.add(node["name"])
            except ValidationError:
                pass

    new_nodes = [dict(k) for k in pinned.kpis]
    combined = [*existing, *new_nodes]
    now_parents = {k["parent_name"] for k in combined if k["parent_name"]}
    for node in existing:  # an existing leaf that now has children becomes a category
        if node["name"] in now_parents and (node.get("weight") is not None or node.get("guidelines")):
            node["weight"], node["guidelines"] = None, {}
            if node["name"] in prev_pins:
                prev_pins[node["name"]].update({"weight": None, "guidelines_hash": None})
            user_w_new.discard(node["name"])
            user_g_new.discard(node["name"])
            notes.append(f'"{node["name"]}" now groups the KPIs you added, so it no longer carries its own weight.')

    scored = [
        k for k in combined if k["name"] not in now_parents and k.get("included_in_scoring", True)
    ]
    existing_scored = [k for k in scored if k["name"] in by_name]
    base = (
        sum(float(k.get("weight") or 0.0) for k in existing_scored) / len(existing_scored)
        if existing_scored and any(k.get("weight") for k in existing_scored)
        else 100.0 / max(len(scored), 1)
    )
    needs = [k for k in scored if k["name"] not in by_name and k.get("weight") is None]
    suggested_new = [k for k in scored if k["name"] not in by_name and k["name"] not in user_w_new and k.get("weight")]
    if suggested_new:  # weighting-step suggestions are relative: put them on the draft's existing scale
        factor = base / (sum(float(k["weight"]) for k in suggested_new) / len(suggested_new))
        for k in suggested_new:
            k["weight"] = float(k["weight"]) * factor
    for k in needs:
        k["weight"] = base
    fixed = {
        k["name"]
        for k in scored
        if k["name"] in user_w_new or (prev_pins.get(k["name"]) or {}).get("weight") is not None
    }
    scaled_fixed = _rebalance_leaves(scored, fixed)
    if scaled_fixed:
        notes.append("To keep the total at 100 I had to scale the weights you supplied proportionally.")
    elif any(k["name"] not in fixed and k["name"] in by_name for k in scored):
        notes.append(
            "I rebalanced the weights I had filled in earlier to make room for your KPIs (yours are unchanged)."
        )
    final_by_name = {k["name"]: k for k in combined}

    pins: dict[str, dict[str, Any]] = {}
    for name, p in prev_pins.items():
        k = final_by_name.get(name)
        if k is None:
            continue
        pins[name] = {**p, "weight": k.get("weight") if p.get("weight") is not None else None}
    for entry in _pin_entries(spec.kpis, new_nodes, user_w_new, user_g_new):
        pins[entry["name"]] = entry
    for ov in spec.overlaps:
        k = final_by_name[ov["name"]]
        old = pins.get(k["name"], {})
        pins[k["name"]] = {
            "name": k["name"], "parent_name": k["parent_name"], "level": k["level"],
            "weight": k.get("weight") if k["name"] in user_w_new else old.get("weight"),
            "guidelines_hash": (
                _guidelines_hash(k.get("guidelines") or {}) if k["name"] in user_g_new else old.get("guidelines_hash")
            ),
        }
    if draft.scoring_formula:
        notes.append(
            "Your custom scoring formula doesn't reference the KPIs you just added — update it if they should count."
        )
    return combined, list(pins.values()), notes


async def ingest_user_kpis(state: BuilderState, config: RunnableConfig) -> dict[str, Any]:
    """Mid-conversation counterpart of `research_kpis`'s first-turn classification (runs on
    every human-input path into `propose_kpis`: after `research_kpis`, `ask_clarification` and
    `respond_conversationally`). When a LATER user message plausibly pastes/introduces a KPI
    list (`_plausibly_contains_kpi_list` gate), runs the same `classify_request` extraction once
    for that message, then pins the KPIs and enriches them (guidelines + weights via
    `_prepare_pinned_kpis`) exactly like first-turn user-specified mode — without re-running the
    open-ended fan-out. Bounded: at most one classify call + one enrichment pass per user message
    (`user_msgs_checked` makes it idempotent), no extra LLM-turn budget, never raises — any
    failure leaves the draft untouched and the prompt-level guidance (plus the model's
    `user_specified_kpis` declaration on update_draft) as the fallback."""
    messages = state.get("messages") or []
    user_count = sum(1 for t in messages if t["role"] == "user")
    checked = state.get("user_msgs_checked")
    if checked is None:  # legacy checkpoint: adopt the current message count as the baseline
        return {"user_msgs_checked": user_count}
    if user_count <= checked:
        return {}
    update: dict[str, Any] = {"user_msgs_checked": user_count}
    last = _last_user_message(messages)
    configurable = config.get("configurable", {})
    bedrock: BedrockClientProtocol | None = configurable.get("bedrock_client")
    if bedrock is None or not plausibly_contains_kpi_list(last):
        return update
    # Same cheap cascade as the first turn: a confident open_ended skips the main-model extraction.
    if (
        await route_request(last, bedrock=bedrock, jev_client=configurable.get("jev_client"), first_turn=False)
    ).skip_extraction:
        return update
    model_id: str | None = configurable.get("chat_model_id")
    session_id: str = state["session_id"]
    turn_started_at: datetime | None = configurable.get("turn_started_at")
    try:
        draft = ScorecardDraft.model_validate(state["draft"])
        context = _conversation_context_text(messages, max_turns=4)
        await emit_turn_event(
            session_id, turn_started_at, "master", "classifying", "Checking your message for a KPI list…"
        )
        spec = await asyncio.to_thread(_classify_request, bedrock, model_id, context, draft.kpis)
        if spec is None:
            await emit_turn_event(session_id, turn_started_at, "master", "mode_detected", "No new KPI list detected.")
            return update
        await emit_turn_event(
            session_id, turn_started_at, "master", "mode_detected",
            f"Detected {len(spec.kpis) + len(spec.overlaps)} KPI(s) in your message — keeping them exactly as given "
            "and filling in the rest.",
        )
        if not any(getattr(draft, f) not in (None, "") for f in HEADER_FIELDS):
            # A KPI list pasted into an EMPTY-header draft: fill the header early (user values win).
            early = await _fill_header(bedrock, last, [n["name"] for n in spec.kpis], spec.scalars)
            spec.scalars = {**early, **spec.scalars}
        pinned = await _prepare_pinned_kpis(
            spec, bedrock, configurable.get("web_search_client"), model_id,
            session_id=session_id, turn_started_at=turn_started_at, conversation_context=context,
            jev_client=configurable.get("jev_client"), standalone_weights=False,
        )
        kpis, pins, notes = _merge_followup_into_draft(draft, state.get("user_spec"), spec, pinned)
        new_draft = {**draft.model_dump(mode="json"), "kpis": kpis}
        for key, value in spec.scalars.items():
            if new_draft.get(key) is None:
                new_draft[key] = value
        if spec.scoring_formula and not draft.scoring_formula:
            check = validate_scoring_formula(spec.scoring_formula, [k["name"] for k in kpis])
            if check.valid:
                new_draft["scoring_formula"] = spec.scoring_formula
            else:
                notes.append(f"I couldn't apply your scoring formula as written ({check.error}).")
        ScorecardDraft.model_validate(new_draft)  # never commit an invalid merge
    except Exception:  # noqa: BLE001 — see docstring: ingestion must never break the turn
        logger.warning("ingest_user_kpis failed; leaving the draft untouched.", exc_info=True)
        await emit_turn_event(
            session_id, turn_started_at, "master", "error", "Couldn't process the KPI list automatically — continuing."
        )
        return update

    prev = state.get("user_spec") or {}
    findings = [*(state.get("research_findings") or []), *(asdict(f) for f in pinned.findings)]
    update.update(
        {
            "draft": new_draft,
            "user_spec": {
                "mode": prev.get("mode") or spec.mode,
                "pinned": pins,
                "notes": notes,
                "allow_additional": bool(prev.get("allow_additional")) or spec.wants_more_kpis,
                "ambiguity_note": spec.ambiguity_note,
                "scoring_formula_hint": spec.scoring_formula or prev.get("scoring_formula_hint"),
            },
            "research_findings": findings or None,
            "messages": [
                {
                    "role": "assistant",
                    "content": (
                        f"[request analysis] the user supplied {len(spec.kpis) + len(spec.overlaps)} KPI(s) in their "
                        "latest message — kept exactly as given; only missing guidelines/weights were filled in."
                        + (" " + " ".join(notes) if notes else "")
                    ),
                }
            ],
        }
    )
    return update


def _name_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {" ".join(v.split()) for v in value if isinstance(v, str) and v.strip()}


def _updated_user_spec(
    user_spec: dict[str, Any] | None,
    new_draft: ScorecardDraft,
    messages: list[ChatTurn],
    allowed_changes: set[str],
    declared_user_kpis: set[str],
) -> dict[str, Any] | None:
    """Returns the (possibly updated) `user_spec` after an accepted `update_draft`; the SAME
    object when nothing changed. (1) Pinned KPIs the user explicitly asked to remove/rename/
    move (`allowed_changes`) are released/updated — that is the clean mechanism for a later
    "remove KPI X". (2) KPIs the model declares the user pasted in THIS conversation
    (`declared_user_kpis`, e.g. a list pasted mid-session after research already ran) are
    pinned too — but only if they really appear in a user message and in the draft, so the
    model can't pin KPIs it invented itself."""
    by_name = {k.name: k for k in new_draft.kpis}
    pinned = [dict(p) for p in (user_spec or {}).get("pinned") or []]
    changed = False

    kept: list[dict[str, Any]] = []
    for p in pinned:
        current = by_name.get(p["name"])
        if p["name"] in allowed_changes:
            if current is None:  # removed/renamed at the user's request: release the pin
                changed = True
                continue
            # moved / reweighted / rewritten at the user's request: the NEW values are now the
            # user's intent, so re-freeze them (only the aspects that were protected before).
            refreshed = {
                **p,
                "parent_name": current.parent_name,
                "level": current.level,
                "weight": current.weight if p.get("weight") is not None else None,
                "guidelines_hash": (
                    _guidelines_hash({k: v.model_dump(mode="json") for k, v in current.guidelines.items()})
                    if p.get("guidelines_hash")
                    else None
                ),
            }
            changed = changed or refreshed != p
            kept.append(refreshed)
            continue
        kept.append(p)

    if declared_user_kpis:
        user_text = _normalize_kpi_name_for_dedup(
            " ".join(t["content"] for t in messages if t["role"] == "user")
        )
        already = {p["name"] for p in kept}
        for name in sorted(declared_user_kpis):
            kpi = by_name.get(name)
            norm = _normalize_kpi_name_for_dedup(name)
            if kpi is None or name in already or not norm or norm not in user_text:
                continue
            kept.append(
                {
                    "name": kpi.name, "parent_name": kpi.parent_name, "level": kpi.level,
                    "weight": None, "guidelines_hash": None,
                }
            )
            changed = True

    if not changed:
        return user_spec
    return {
        "mode": (user_spec or {}).get("mode", "mid_conversation"),
        "notes": (user_spec or {}).get("notes", []),
        "allow_additional": (user_spec or {}).get("allow_additional", False),
        "ambiguity_note": None,
        "scoring_formula_hint": (user_spec or {}).get("scoring_formula_hint"),
        "pinned": kept,
    }


def _find_kpi(kpis: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    exact = next((k for k in kpis if k["name"] == name), None)
    if exact is not None:
        return exact
    norm = _normalize_kpi_name_for_dedup(name)
    return next((k for k in kpis if _normalize_kpi_name_for_dedup(k["name"]) == norm), None) if norm else None


def _scored_leaves(kpis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parents = {k.get("parent_name") for k in kpis if k.get("parent_name")}
    return [k for k in kpis if k["name"] not in parents and k.get("included_in_scoring", True)]


def _descendants(kpis: list[dict[str, Any]], name: str) -> set[str]:
    out: set[str] = set()
    frontier = {name}
    while frontier:
        children = {k["name"] for k in kpis if k.get("parent_name") in frontier} - out
        out |= children
        frontier = children
    return out


_RESTRUCTURE_INTENT = re.compile(
    r"\b(start over|from scratch|replace (?:all|everything)|redo|completely (?:new|different)|restructure|"
    r"rebuild|new set of|different set of)\b",
    re.IGNORECASE,
)

_OTHERS_PHRASE = re.compile(
    r"\b(the others|all others|all the others|everything else|the rest|the remaining|other kpis|"
    r"scale (?:the )?(?:others|rest)|proportionally)\b",
    re.IGNORECASE,
)


def _normalize_edit_ops(ops: list[Any], messages: list[ChatTurn]) -> list[Any]:
    """When the user asked to change ONE KPI and scale/rebalance "the others", the model sometimes
    lists every other KPI as its own `set_weight` op. Those ops are only the rebalance's side effects
    (the server computes them exactly), so they are dropped; only set_weight ops whose KPI the user's
    latest message (or, for an unnamed "that KPI", the assistant message before it) actually names are
    kept — with proportional rebalancing. Other op kinds are untouched."""
    last_user = max((i for i, t in enumerate(messages) if t["role"] == "user"), default=None)
    if last_user is None:
        return ops
    text = messages[last_user]["content"]
    if not _OTHERS_PHRASE.search(text):
        return ops
    window = text
    if _ANAPHORA.search(text):
        previous = next((t for t in reversed(messages[:last_user]) if t["role"] == "assistant"), None)
        if previous is not None:
            window = f"{previous['content']} {text}"
    padded = f" {_normalize_kpi_name_for_dedup(window)} "
    weight_ops = [o for o in ops if isinstance(o, dict) and o.get("op") == "set_weight"]
    named = [o for o in weight_ops if f" {_normalize_kpi_name_for_dedup(str(o.get('name') or ''))} " in padded]
    if not weight_ops or not named:
        return ops
    keep = {id(o) for o in named}
    out: list[Any] = []
    for o in ops:
        if isinstance(o, dict) and o.get("op") == "set_weight":
            if id(o) in keep:
                out.append({**o, "rebalance": "proportional"})
            continue
        out.append(o)
    return out


def _fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def _clean_explanation(text: str | None) -> str | None:
    """The model's prose is kept only if it is number-free and asks nothing: its numbers are recollections
    (they drifted from the saved weights) and the closing question is the server's single one."""
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [x for x in sentences if x and not re.search(r"\d|%|\?", x)]
    out = " ".join(kept).strip()
    return out if out and out not in _GENERIC_NOTES else None


def _edit_summary(
    old: list[dict[str, Any]] | dict[str, Any],
    new: list[dict[str, Any]],
    targeted: set[str],
    explanation: str | None,
) -> str:
    """The user-visible text of an applied `edit_kpis`, built from the REAL before/after weights (the
    model's own prose quotes numbers from memory and drifts from what was saved). The model's
    explanation is kept only when it contains no digits."""
    old_w = dict(old) if isinstance(old, dict) else {k["name"]: k.get("weight") for k in old}
    new_w = {k["name"]: k.get("weight") for k in new}
    added = [n for n in new_w if n not in old_w]
    removed = [n for n in old_w if n not in new_w]
    changed = [
        (n, float(old_w[n]), float(new_w[n]))
        for n in new_w
        if n in old_w and old_w[n] is not None and new_w[n] is not None
        and abs(float(old_w[n]) - float(new_w[n])) >= 0.005
    ]
    direct = [c for c in changed if c[0] in targeted]
    scaled = [c for c in changed if c[0] not in targeted]
    lines = ["I applied your change."]
    cleaned = _clean_explanation(explanation)
    if cleaned:
        lines = [cleaned]
    for n, a, b in direct:
        lines.append(f"- {n}: {_fmt_pct(a)} → {_fmt_pct(b)}")
    if scaled:
        biggest = sorted(scaled, key=lambda c: abs(c[2] - c[1]), reverse=True)[:3]
        lines.append(
            f"- {len(scaled)} other KPI weight(s) were rescaled to keep the total at 100%, e.g. "
            + "; ".join(f"{n} {_fmt_pct(a)} → {_fmt_pct(b)}" for n, a, b in biggest)
            + "."
        )
    if added:
        lines.append("- Added: " + ", ".join(added[:5]) + (f" (+{len(added) - 5} more)" if len(added) > 5 else ""))
    if removed:
        lines.append(
            "- Removed: " + ", ".join(removed[:5]) + (f" (+{len(removed) - 5} more)" if len(removed) > 5 else "")
        )
    leaves = _scored_leaves(new)
    total = sum(float(k.get("weight") or 0) for k in leaves)
    lines.append(f"- Total weight of the scored KPIs: {_fmt_pct(total)}.")
    lines.append("\nWould you like to save the scorecard as it is now, or change anything else?")
    return _cap_summary("\n".join(lines))


def _short_names(names: set[str] | list[str], limit: int = 3) -> str:
    ordered = sorted(names)
    shown = ", ".join(f'"{n}"' for n in ordered[:limit])
    return shown + (f" and {len(ordered) - limit} more" if len(ordered) > limit else "")


def _apply_kpi_ops(
    kpis: list[dict[str, Any]], ops: list[Any], generated: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], set[str], set[str]]:
    """Deterministically applies `edit_kpis` operations to a copy of `kpis` (KpiDraft dicts).
    Returns `(new_kpis, errors, targeted_names, weight_changed_names)`: `errors` non-empty means
    nothing may be committed; `targeted_names` are the EXISTING KPIs the ops act on directly (used to
    verify edits of user-pinned KPIs); `weight_changed_names` every KPI whose final weight differs
    (including those only rescaled by the rebalance). Leaf weights are rescaled with exact
    arithmetic to total 100 (`_rebalance_leaves`: only the non-fixed leaves move, rounded to 2
    decimals with the drift on the largest). Guidelines for added/regenerated KPIs come from
    `generated` (written by the compact-rubric machinery), never from the main model."""
    work = [{**k, "guidelines": dict(k.get("guidelines") or {})} for k in kpis]
    original_weights = {k["name"]: k.get("weight") for k in work}
    errors: list[str] = []
    targeted: set[str] = set()
    fixed: set[str] = set()
    needs_rebalance = False
    skip_rebalance_for_none = False

    def level_of(parent: str | None) -> int:
        if parent is None:
            return 1
        found = _find_kpi(work, parent)
        return (found["level"] + 1) if found else 1

    for raw in ops:
        if not isinstance(raw, dict):
            errors.append("each op must be an object")
            continue
        op = raw.get("op")
        name = str(raw.get("name") or "").strip()
        if op == "add":
            if not name:
                errors.append("add needs a name")
                continue
            if _find_kpi(work, name) is not None:
                errors.append(f'cannot add "{name}": a KPI with that name already exists')
                continue
            parent_raw = raw.get("parent_name")
            parent = None
            if parent_raw:
                parent_kpi = _find_kpi(work, str(parent_raw))
                if parent_kpi is None:
                    errors.append(f'cannot add "{name}": parent "{parent_raw}" does not exist')
                    continue
                parent = parent_kpi["name"]
                if parent_kpi["level"] + 1 > MAX_HIERARCHY_LEVEL:
                    errors.append(f'cannot add "{name}": maximum hierarchy depth exceeded')
                    continue
                if not any(k.get("parent_name") == parent for k in work):  # a leaf becomes a category
                    parent_kpi["weight"] = None
                    parent_kpi["guidelines"] = {}
            weight = raw.get("weight")
            new = {
                "name": name, "weight": float(weight) if weight is not None else None, "level": level_of(parent),
                "parent_name": parent, "included_in_scoring": True, "guidelines": dict(generated.get(name) or {}),
            }
            work.append(new)
            if new["weight"] is not None:
                fixed.add(name)
            else:
                existing = _scored_leaves(work)
                new["weight"] = round(100.0 / max(1, len(existing)), 2)
                fixed.add(name)
            needs_rebalance = True
            continue

        target = _find_kpi(work, name)
        if target is None:
            errors.append(f'unknown KPI "{name}" (use the exact name from the current draft)')
            continue
        targeted.add(target["name"])
        if op == "set_weight":
            if raw.get("weight") is None:
                errors.append(f'set_weight "{name}" needs a weight')
                continue
            if any(k.get("parent_name") == target["name"] for k in work):
                errors.append(f'"{name}" is a category — only leaf KPIs carry a weight')
                continue
            target["weight"] = float(raw["weight"])
            if raw.get("rebalance", "proportional") == "none":
                skip_rebalance_for_none = True
            else:
                fixed.add(target["name"])
                needs_rebalance = True
        elif op == "rename":
            new_name = str(raw.get("new_name") or "").strip()
            if not new_name:
                errors.append(f'rename "{name}" needs new_name')
                continue
            if new_name != target["name"] and _find_kpi(work, new_name) is not None:
                errors.append(f'cannot rename "{name}" to "{new_name}": that name is already used')
                continue
            old = target["name"]
            for k in work:
                if k.get("parent_name") == old:
                    k["parent_name"] = new_name
            target["name"] = new_name
            if old in fixed:
                fixed.discard(old)
                fixed.add(new_name)
            if old in generated:
                generated[new_name] = generated[old]
        elif op == "remove":
            drop = {target["name"]} | _descendants(work, target["name"])
            work = [k for k in work if k["name"] not in drop]
            fixed -= drop
            needs_rebalance = True
            # a category left without children becomes an ordinary (guideline-less) leaf: drop it too
            parent = target.get("parent_name")
            if parent and not any(k.get("parent_name") == parent for k in work):
                work = [k for k in work if k["name"] != parent]
        elif op == "move":
            parent_raw = raw.get("parent_name")
            parent = None
            if parent_raw:
                parent_kpi = _find_kpi(work, str(parent_raw))
                if parent_kpi is None:
                    errors.append(f'cannot move "{name}": parent "{parent_raw}" does not exist')
                    continue
                if parent_kpi["name"] == target["name"] or parent_kpi["name"] in _descendants(work, target["name"]):
                    errors.append(f'cannot move "{name}" under itself or its own descendant')
                    continue
                parent = parent_kpi["name"]
                if not any(k.get("parent_name") == parent for k in work):
                    parent_kpi["weight"] = None
                    parent_kpi["guidelines"] = {}
            target["parent_name"] = parent
            stack = [target["name"]]
            target["level"] = level_of(parent)
            while stack:
                cur = stack.pop()
                cur_level = next(k["level"] for k in work if k["name"] == cur)
                for k in work:
                    if k.get("parent_name") == cur:
                        k["level"] = cur_level + 1
                        stack.append(k["name"])
            if any(k["level"] > MAX_HIERARCHY_LEVEL for k in work):
                errors.append(f'moving "{name}" would exceed the maximum hierarchy depth')
            needs_rebalance = True
        elif op == "set_included_in_scoring":
            target["included_in_scoring"] = bool(raw.get("included_in_scoring", True))
            needs_rebalance = True
        elif op == "set_guidelines":
            fresh = generated.get(name) or generated.get(target["name"])
            if not fresh:
                errors.append(f'could not regenerate the guidelines of "{name}"')
                continue
            target["guidelines"] = dict(fresh)
        else:
            errors.append(f"unknown op {op!r}")

    if not errors and needs_rebalance:
        leaves = _scored_leaves(work)
        if leaves:
            # a leaf that just became scored (or new) without a weight gets an equal share first
            for leaf in leaves:
                if leaf.get("weight") is None:
                    leaf["weight"] = round(100.0 / len(leaves), 2)
            if sum(float(lf["weight"]) for lf in leaves if lf["name"] in fixed) > 100.0 + 0.01:
                errors.append("the weights you fixed already total more than 100")
            elif not (skip_rebalance_for_none and not fixed):
                _rebalance_leaves(leaves, fixed)
    changed = {
        k["name"] for k in work
        if k["name"] in original_weights and k.get("weight") != original_weights[k["name"]]
    }
    return work, errors, targeted, changed


def update_draft(state: BuilderState) -> dict[str, Any]:
    tool_input = (state.get("pending_tool") or {}).get("input") or {}
    patch = tool_input.get("patch") if isinstance(tool_input, dict) else None
    if not isinstance(patch, dict):
        patch = {}
    # `scoring_formula` can ONLY be changed via the dedicated, validated
    # `update_scoring_formula` tool/node (below) — defensively strip it here in case the
    # model smuggles it into an update_draft patch anyway, so an unvalidated formula can
    # never reach draft state through the wrong door.
    patch = {k: v for k, v in patch.items() if k != "scoring_formula"}
    edit_mode = (state.get("pending_tool") or {}).get("name") == "edit_kpis"
    edit_changed_weights: set[str] = set()
    edit_targeted: set[str] = set()
    if edit_mode:
        base = ScorecardDraft.model_validate(state["draft"])
        ops = tool_input.get("ops") if isinstance(tool_input, dict) else None
        if isinstance(ops, list):
            ops = _normalize_edit_ops(ops, state.get("messages") or [])
        new_kpis, op_errors, edit_targeted, edit_changed_weights = _apply_kpi_ops(
            [k.model_dump(mode="json") for k in base.kpis],
            ops if isinstance(ops, list) else [],
            dict(tool_input.get("_generated") or {}),
        )
        if op_errors:
            error_summary = "; ".join(op_errors[:3]) + (f" (+{len(op_errors) - 3} more)" if len(op_errors) > 3 else "")
            return {
                "pending_tool": None,
                "last_patch_error": error_summary,
                "status": "gathering",
                "messages": [{"role": "tool", "content": f"edit_kpis REJECTED: {error_summary}"}],
            }
        patch = {"kpis": new_kpis}
    if isinstance(patch.get("kpis"), list) and not edit_mode:
        # Same GLM-5 shape gap `_normalize_kpi_guidelines` repairs for propose_kpi_batch
        # (see that function's docstring) can occur here too — repair it BEFORE
        # validation so a real, otherwise-good patch isn't REJECTED (wasting a turn) over
        # a guideline rung nested one level too shallow.
        patch = {
            **patch,
            "kpis": [
                _normalize_kpi_guidelines(kpi) if isinstance(kpi, dict) else kpi for kpi in patch["kpis"]
            ],
        }
    confirmed_flag = bool(tool_input.get("confirmed", False)) if isinstance(tool_input, dict) else False
    # The model's `user_requested_kpi_changes` is only a CLAIM — verified against the user's
    # actual messages (see `_verified_user_changes`); only verified names are exempt from the
    # pinned-KPI checks below.
    declared_changes = _name_set(
        tool_input.get("user_requested_kpi_changes") if isinstance(tool_input, dict) else None
    )
    allowed_changes, unverified_changes = _verified_user_changes(declared_changes, state.get("messages") or [])
    declared_user_kpis = _name_set(tool_input.get("user_specified_kpis") if isinstance(tool_input, dict) else None)

    current = ScorecardDraft.model_validate(state["draft"])
    if isinstance(patch.get("kpis"), list) and not edit_mode:
        # A full replacement that omits/shortens a KPI's rubric must not wipe the existing one:
        # carry the current guidelines over for KPIs whose name matches.
        old_by_name = {k.name: k for k in current.kpis}
        carried: list[Any] = []
        for item in patch["kpis"]:
            old = old_by_name.get(item.get("name")) if isinstance(item, dict) else None
            given = item.get("guidelines") if isinstance(item, dict) else None
            if old is not None and old.guidelines and (not isinstance(given, dict) or len(given) < len(old.guidelines)):
                item = {**item, "guidelines": {k: v.model_dump(mode="json") for k, v in old.guidelines.items()}}
            carried.append(item)
        patch = {**patch, "kpis": carried}
    merged = {**current.model_dump(mode="json"), **patch}

    try:
        new_draft = ScorecardDraft.model_validate(merged)
    except ValidationError as exc:
        # Reject the patch — state["draft"] is left untouched. The error is surfaced to
        # the model as a "tool" turn so its next propose_kpis call can see exactly what
        # was wrong and retry with a corrected patch. The draft in LangGraph state is
        # never allowed to become an invalid ScorecardDraft.
        error_summary = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return {
            "pending_tool": None,
            "last_patch_error": error_summary,
            "status": "gathering",
            "messages": [
                {
                    "role": "tool",
                    "content": f"update_draft REJECTED (invalid patch): {error_summary}",
                }
            ],
        }

    if "kpis" in patch and not edit_mode and current.kpis and not (state.get("user_spec") or {}).get("pinned"):
        new_names = {k.name for k in new_draft.kpis}
        dropped = {k.name for k in current.kpis if k.name not in new_names}
        if len(dropped) >= 3:
            last_user_text = next(
                (t["content"] for t in reversed(state.get("messages") or []) if t["role"] == "user"), ""
            )
            verified, _unverified = _verified_user_changes(dropped, state.get("messages") or [])
            if len(dropped - verified) >= 3 and not _RESTRUCTURE_INTENT.search(last_user_text):
                error_summary = (
                    f"this patch would silently drop {len(dropped)} existing KPIs ({_short_names(dropped)}). "
                    "Use `edit_kpis` (remove ops) for removals, or omit `kpis` from the patch to keep them all."
                )
                return {
                    "pending_tool": None,
                    "last_patch_error": error_summary,
                    "status": "gathering",
                    "messages": [{"role": "tool", "content": f"update_draft REJECTED: {error_summary}"}],
                }

    # --- Pinned (user-specified) KPI protection: see `_pinned_violations`. Enforced here, in
    # code, because a prompt-only "preserve the user's KPIs" instruction is exactly what
    # silently decays over a long conversation.
    user_spec = state.get("user_spec")
    pinned: list[dict[str, Any]] = list((user_spec or {}).get("pinned") or [])
    if edit_mode and pinned:
        # Direct targets that are user-pinned need the user's own request (verified server-side);
        # weights merely rescaled by the rebalance are then allowed to move with it.
        pinned_names = {p["name"] for p in pinned}
        verified_targets, unverified_targets = _verified_user_changes(
            edit_targeted & pinned_names, state.get("messages") or []
        )
        if unverified_targets:
            error_summary = (
                "the user did not ask to change " + _short_names(unverified_targets)
                + ". Send ONE op per KPI the user NAMED (set_weight with rebalance 'proportional' already "
                "rescales all the others — do not list them)."
            )
            return {
                "pending_tool": None,
                "last_patch_error": error_summary,
                "status": "gathering",
                "messages": [{"role": "tool", "content": f"edit_kpis REJECTED: {error_summary}"}],
            }
        allowed_changes = allowed_changes | verified_targets | (edit_changed_weights & pinned_names)
    if "kpis" in patch and pinned:
        problems = _pinned_violations(pinned, new_draft.kpis, allowed_changes)
        if problems:
            error_summary = (
                "user-specified KPIs must be preserved exactly: " + "; ".join(problems) + ". Re-send `kpis` "
                "including every one of them with its exact name, parent_name and (user-supplied) weight/"
                "guidelines — when adding or removing KPIs, rebalance by changing ONLY weights you filled in "
                "yourself, and tell the user. Only if the user's latest message EXPLICITLY asked for this change "
                "(naming the KPI), list the ORIGINAL name(s) in `user_requested_kpi_changes`."
            )
            if unverified_changes:
                error_summary += (
                    " NOTE: the server could not find the user asking for a change to "
                    + ", ".join(f'"{n}"' for n in sorted(unverified_changes))
                    + " in their latest message, so `user_requested_kpi_changes` was ignored for them. If you "
                    "believe they want it, ask via ask_clarification (name the KPI and the change) first."
                )
            return {
                "pending_tool": None,
                "last_patch_error": error_summary,
                "status": "gathering",
                "messages": [{"role": "tool", "content": f"update_draft REJECTED: {error_summary}"}],
            }
    new_user_spec = _updated_user_spec(
        user_spec, new_draft, state.get("messages") or [], allowed_changes, declared_user_kpis
    )

    if confirmed_flag and new_draft.is_complete():
        new_status = "ready_to_confirm"
        generic_note = "Draft updated and confirmed complete."
    elif confirmed_flag:
        new_status = "gathering"
        generic_note = f"Cannot confirm — draft still missing: {new_draft.missing_fields()}"
    else:
        new_status = "gathering"
        generic_note = "Draft updated."

    # Prefer the model's own real, descriptive `assistant_message` (see UPDATE_DRAFT_TOOL)
    # over the generic note above. This matters even though propose_kpis (the caller) ALSO
    # computes its own assistant_note from the same field: `_build_turn_result` picks the
    # LAST "assistant"/"tool"-role message in state["messages"] as the turn's visible text,
    # and THIS node's message is always appended after propose_kpis's — so without this,
    # the generic note below would silently win and hide the model's real explanation on
    # every single-LLM-turn human turn (e.g. any confirming update_draft, or a
    # non-confirming one not immediately followed by another node that also sets a real
    # message) — exactly the "described it but the UI shows a robotic label instead" gap.
    note = str(tool_input.get("assistant_message") or "").strip() or generic_note

    update: dict[str, Any] = {
        "draft": new_draft.model_dump(mode="json"),
        "pending_tool": None,
        "last_patch_error": None,
        "status": new_status,
        "messages": [{"role": "tool", "content": note}],
    }
    if edit_mode and new_status != "ready_to_confirm":
        # An applied edit ENDS the turn: the visible text is built from the real before/after weights
        # and the graph pauses (respond_conversationally) instead of looping back to the model, which
        # used to re-issue the same edit until the visit cap.
        baseline = state.get("turn_base_weights")
        summary = _edit_summary(
            baseline if isinstance(baseline, dict) and baseline else [k.model_dump(mode="json") for k in current.kpis],
            [k.model_dump(mode="json") for k in new_draft.kpis],
            edit_targeted,
            str(tool_input.get("assistant_message") or ""),
        )
        update["pending_tool"] = {"name": "respond_conversationally", "input": {"response": summary}}
        update["messages"] = [{"role": "tool", "content": summary, "kind": "edit_summary"}]
    if new_user_spec is not user_spec:
        update["user_spec"] = new_user_spec
    return update


def _route_after_update(state: BuilderState) -> str:
    if state.get("status") == "ready_to_confirm":
        return "confirm"
    if (state.get("pending_tool") or {}).get("name") == "respond_conversationally":
        return "respond_conversationally"
    return "propose_kpis"


def update_scoring_formula(state: BuilderState) -> dict[str, Any]:
    """Handles the `update_scoring_formula` tool call (see `UPDATE_SCORING_FORMULA_TOOL`
    and Part 2's custom-formula feature) — validated with the SAME `app/ai/scoring_formula
    .validate` used by the `POST /scorecards/.../validate-formula` endpoint and the
    manual-materialization path, so the LLM builder can never persist a formula that would
    later fail at materialize/evaluate time. An invalid formula is REJECTED exactly like an
    invalid `update_draft` patch (a "tool" turn explaining what was wrong, draft
    untouched) and always routes back to `propose_kpis` for another turn — never completes
    the draft by itself (mirrors `update_draft`'s own non-confirming case)."""
    tool_input = (state.get("pending_tool") or {}).get("input") or {}
    formula = tool_input.get("formula") if isinstance(tool_input, dict) else None
    formula = formula if (formula is None or isinstance(formula, str)) else str(formula)

    current = ScorecardDraft.model_validate(state["draft"])
    available_names = [kpi.name for kpi in current.kpis]
    result = validate_scoring_formula(formula, available_names)

    if not result.valid:
        return {
            "pending_tool": None,
            "last_patch_error": result.error,
            "messages": [
                {
                    "role": "tool",
                    "content": f"update_scoring_formula REJECTED (invalid formula): {result.error}",
                }
            ],
        }

    generic_note = (
        f"Scoring formula set: {formula!r}"
        + (f" (note: unused KPI(s) not referenced: {', '.join(result.unused_kpis)})" if result.unused_kpis else "")
        if formula
        else "Scoring formula cleared — reverted to the default weighted average."
    )
    # Same reasoning as update_draft's own assistant_message preference above — this
    # node's message is what `_build_turn_result` actually shows the user (it's appended
    # after propose_kpis's own note), so without this, the model's real explanation would
    # be silently replaced by the generic note whenever this happens to be the last
    # message written in the turn.
    note = str(tool_input.get("assistant_message") or "").strip() or generic_note
    new_draft = current.model_copy(update={"scoring_formula": formula})
    return {
        "draft": new_draft.model_dump(mode="json"),
        "pending_tool": None,
        "last_patch_error": None,
        "messages": [{"role": "tool", "content": note}],
    }


def confirm(state: BuilderState) -> dict[str, Any]:
    return {"status": "confirmed"}


# --- Graph assembly ----------------------------------------------------------------------


def build_graph_definition() -> StateGraph:
    graph = StateGraph(BuilderState)
    graph.add_node("gather_info", gather_info)
    graph.add_node("check_similarity", check_similarity)
    graph.add_node("suggest_similar", suggest_similar)
    graph.add_node("research_kpis", research_kpis)
    graph.add_node("ingest_user_kpis", ingest_user_kpis)
    graph.add_node("propose_kpis", propose_kpis)
    graph.add_node("ask_clarification", ask_clarification)
    graph.add_node("respond_conversationally", respond_conversationally)
    graph.add_node("update_draft", update_draft)
    graph.add_node("update_scoring_formula", update_scoring_formula)
    graph.add_node("confirm", confirm)

    graph.add_edge(START, "gather_info")
    graph.add_edge("gather_info", "check_similarity")
    graph.add_conditional_edges(
        "check_similarity",
        _route_after_similarity,
        {"suggest_similar": "suggest_similar", "research_kpis": "research_kpis"},
    )
    graph.add_edge("suggest_similar", "research_kpis")
    # research_kpis (the multi-agent research fan-out master step — see its own docstring)
    # always falls straight through to propose_kpis: it is a no-op after the first time it
    # runs in a session (guarded by research_done), so it never introduces its own branch
    # here — the routing decision already happened one hop earlier, in
    # _route_after_similarity, and research_kpis unconditionally continues the same turn.
    # Every human-input path into propose_kpis (research_kpis, and the ask_clarification /
    # respond_conversationally resumes below) passes through ingest_user_kpis first — a cheap
    # no-op unless the newest user message plausibly pastes a KPI list (see that node).
    graph.add_edge("research_kpis", "ingest_user_kpis")
    graph.add_edge("ingest_user_kpis", "propose_kpis")
    graph.add_conditional_edges(
        "propose_kpis",
        _route_after_propose,
        {
            "ask_clarification": "ask_clarification",
            "update_draft": "update_draft",
            "update_scoring_formula": "update_scoring_formula",
            "respond_conversationally": "respond_conversationally",
        },
    )
    graph.add_edge("ask_clarification", "ingest_user_kpis")
    # respond_conversationally mirrors ask_clarification's own edge exactly (see its
    # docstring): it pauses via interrupt(), then resumes straight back into propose_kpis
    # once the user's next real message arrives — never routes anywhere else.
    graph.add_edge("respond_conversationally", "ingest_user_kpis")
    graph.add_conditional_edges(
        "update_draft",
        _route_after_update,
        {
            "confirm": "confirm",
            "propose_kpis": "propose_kpis",
            "respond_conversationally": "respond_conversationally",
        },
    )
    # update_scoring_formula never completes the draft by itself (mirrors update_draft's
    # own non-confirming case — see its own docstring) — always loops back for another turn.
    graph.add_edge("update_scoring_formula", "propose_kpis")
    graph.add_edge("confirm", END)
    return graph


def _psycopg_conn_string(database_url: str) -> str:
    """LangGraph's `AsyncPostgresSaver` uses psycopg directly (`postgresql://...`), not
    SQLAlchemy's `postgresql+psycopg://...` dialect URL — strip the `+psycopg` suffix."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


# Tracks which database URLs have already had `AsyncPostgresSaver.setup()` run
# successfully in this process — see `GraphManager._ensure_ready`.
_checkpoint_schema_ready: set[str] = set()


class GraphManager:
    """Owns the single long-lived `AsyncPostgresSaver` connection (and the graph compiled
    against it) for the process lifetime. `AsyncPostgresSaver.setup()` creates its own
    checkpoint tables (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`,
    `checkpoint_migrations`) — these are managed by langgraph itself, not Alembic; calling
    `.setup()` is idempotent, so it is safe to call once per process start."""

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url = database_url or get_settings().database_url
        self._stack: AsyncExitStack | None = None
        self._saver: AsyncPostgresSaver | None = None
        self._compiled = None
        self._init_lock: asyncio.Lock | None = None

    async def _ensure_ready(self) -> None:
        if self._compiled is not None:
            return
        # Serialize first-use initialization: background chat turns (and a request's own state
        # read) can reach this concurrently on a cold process; two racing initializations would
        # each open a saver connection and one would overwrite (and orphan/close) the other.
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._compiled is not None:
                return
            await self._initialize()

    async def _initialize(self) -> None:
        self._stack = AsyncExitStack()
        self._saver = await self._stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(_psycopg_conn_string(self._database_url))
        )
        # `.setup()`'s DDL (CREATE TABLE/INDEX ... IF NOT EXISTS) only needs to succeed
        # once per *database*, not once per connection/GraphManager instance. In a real
        # ASGI process this is moot (GraphManager is a true process-lifetime singleton —
        # see get_graph_manager — so _ensure_ready only ever runs once regardless). But
        # the test harness intentionally forces a brand-new GraphManager, and therefore a
        # brand-new AsyncPostgresSaver connection, on every single test (see
        # tests/conftest.py::_reset_ai_graph_manager's docstring: a psycopg async
        # connection can't cross event loops, and different tests may run under
        # different ones). Re-running `.setup()` on every one of those resets means every
        # chat-builder test re-enters Postgres's `CREATE INDEX CONCURRENTLY` protocol
        # (which must wait for any concurrent transaction across the whole database to
        # finish) for no reason once the index already exists — unnecessary contention
        # with whatever else the test process happens to have open at that moment,
        # observed to occasionally stall a test run entirely. Skip it once this process
        # has already confirmed the schema exists for this database.
        if self._database_url not in _checkpoint_schema_ready:
            await self._saver.setup()
            _checkpoint_schema_ready.add(self._database_url)
        self._compiled = build_graph_definition().compile(checkpointer=self._saver)

    async def get_compiled_graph(self):
        await self._ensure_ready()
        return self._compiled

    async def adelete_thread(self, thread_id: str) -> None:
        """Deletes every checkpoint row for `thread_id` — see module-level
        `delete_session_checkpoints`, the only caller."""
        await self._ensure_ready()
        assert self._saver is not None  # guaranteed by _ensure_ready
        await self._saver.adelete_thread(thread_id)

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._saver = None
        self._compiled = None
        self._init_lock = None


_graph_manager: GraphManager | None = None


def get_graph_manager() -> GraphManager:
    global _graph_manager
    if _graph_manager is None:
        _graph_manager = GraphManager()
    return _graph_manager


# --- High-level orchestration, used by app/api/v1/chat.py -------------------------------


@dataclass
class BuilderTurnResult:
    """Normalized result of one graph turn, for the API layer to serialize."""

    status: str  # "awaiting_clarification" | "awaiting_similar_choice" | "gathering" | "confirmed"
    draft: dict[str, Any]
    question: dict[str, Any] | None
    assistant_note: str | None
    # Populated only when status == "awaiting_similar_choice" (see check_similarity /
    # suggest_similar above) — reuse-suggestion cards for the frontend to render.
    similar_suggestions: list[dict[str, Any]] | None = None


_GENERIC_NOTES = frozenset(
    {
        "Draft updated.",
        "Updating the draft.",
        "Draft updated and confirmed complete.",
        "Confirming and saving the draft.",
    }
)


def _text_tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z]{3,}", text.lower()))


def _near_duplicate_text(a: str, b: str) -> bool:
    """Same paragraph up to numbers/punctuation/wording drift: token Jaccard >= 0.8, or one token set
    contained in the other."""
    ta, tb = _text_tokens(a), _text_tokens(b)
    if not ta or not tb:
        return a.strip() == b.strip()
    inter = len(ta & tb)
    return inter / len(ta | tb) >= 0.8 or inter == min(len(ta), len(tb)) and min(len(ta), len(tb)) >= 6


_CLOSING_Q = re.compile(r"[^.!?\n]*\?\s*$")
_SAVE_ADJUST = re.compile(r"\b(save|adjust|change anything|anything else|as-is|as is)\b", re.IGNORECASE)


def _dedupe_closing_questions(notes: list[str]) -> list[str]:
    """When several notes end with a 'save it / adjust anything?' style question, only the LAST keeps its
    question; the earlier ones lose that trailing sentence (a note that was only the question is dropped)."""
    def closing(n: str) -> str | None:
        m = _CLOSING_Q.search(n.strip())
        return m.group(0).strip() if m else None

    last_idx = None
    for i in range(len(notes) - 1, -1, -1):
        q = closing(notes[i])
        if q:
            last_idx = i
            break
    if last_idx is None:
        return notes
    last_q = closing(notes[last_idx]) or ""
    out: list[str] = []
    for i, n in enumerate(notes):
        q = closing(n) if i < last_idx else None
        if q and (
            (_SAVE_ADJUST.search(q) and _SAVE_ADJUST.search(last_q))
            or _near_duplicate_text(q, last_q)
        ):
            n = n.strip()[: -len(q)].rstrip()
        if n:
            out.append(n)
    return out


def _answers_already(note: str, answer: str) -> bool:
    """True when the model's own note already contains (most of) the side-question answer."""
    ta, tb = _text_tokens(note), _text_tokens(answer)
    return bool(tb) and len(ta & tb) / len(tb) >= 0.6


_SYSTEM_PREFIX = re.compile(r"^\s*\[system[^\]]*\]\s*", re.IGNORECASE)


def _strip_system_prefix(text: str) -> str:
    """Removes a leading '[system note] ' / '[system] ' tag (the model sometimes echoes it back)."""
    return _SYSTEM_PREFIX.sub("", text, count=1) if isinstance(text, str) else text


_INTERNAL_NOTE = re.compile(
    r"^(\[|update_draft|edit_kpis|update_scoring_formula|Cannot confirm|Your previous|\(The assistant didn't)",
    re.IGNORECASE,
)
_STALE_NOTE = re.compile(
    r"^(I'm sorry|I am sorry|Sorry,|My last response was too long)|encountered an issue|rephrase your request",
    re.IGNORECASE,
)


def _visible_turn_notes(messages: list[dict[str, Any]]) -> str | None:
    """ALL assistant-visible text produced since the user's latest message, in order, joined into
    one bubble: e.g. `update_draft`'s explanation (answering the user's side question) followed by
    the `ask_clarification` question. NEVER included: internal/tool notes (`[...]`, `... REJECTED`,
    `Cannot confirm`, ...), notes of an attempt that a later tool message REJECTED (superseded), and
    stale apology/fallback texts when a real note exists. Duplicates are collapsed; a generic
    placeholder ("Draft updated.") is used only when it is the sole note."""
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            start = i + 1
            break
    summaries = [m for m in messages[start:] if m.get("kind") == "edit_summary" and m.get("content")]
    if summaries:
        # An applied edit: the visible text is ONLY the server-built summary (real before/after values and
        # one closing question) — any model paragraph about the same edit quotes remembered numbers.
        return str(summaries[-1]["content"]).strip()
    entries: list[str | None] = []  # None = removed
    for turn in messages[start:]:
        role = turn.get("role")
        if role not in ("assistant", "tool"):
            continue
        content = str(turn.get("content") or "").strip()
        if turn.get("kind") != "edit_summary" and re.match(r"^\[system(?: note)?\]\s+\S", content, re.IGNORECASE):
            # an echoed '[system note] <real text>' keeps its text; bare internal system notices are dropped below
            if not re.match(r"^\[system\]\s+(Your previous|The user asked|Do NOT)", content, re.IGNORECASE):
                content = _strip_system_prefix(content).strip()
        if not content:
            continue
        if role == "tool" and re.match(r"^(update_draft|edit_kpis|update_scoring_formula) REJECTED", content):
            for j in range(len(entries) - 1, -1, -1):  # the attempt that was rejected is superseded
                if entries[j] is not None:
                    entries[j] = None
                    break
            continue
        if _INTERNAL_NOTE.match(content):
            continue
        entries.append(content)
    notes: list[str] = []
    generic: list[str] = []
    for content in (e for e in entries if e is not None):
        if content in _GENERIC_NOTES:
            generic.append(content)
            continue
        dup = next((i for i, n in enumerate(notes) if _near_duplicate_text(n, content)), None)
        if dup is None:
            notes.append(content)
        elif len(content) >= len(notes[dup]) or True:
            notes[dup] = content  # the later note supersedes the earlier near-identical one
    notes = _dedupe_closing_questions(notes)
    real = [n for n in notes if not _STALE_NOTE.search(n)]
    if real:
        return "\n\n".join(real)
    if notes:
        return notes[-1]
    if generic:
        return generic[-1]
    return None


def _without_card_question(note: str, card_question: str) -> str:
    """When the turn returns a structured `question` (the card with option chips), that card is the ONLY place
    the question appears: the bubble text loses the paragraph equal to it and any trailing save/adjust-style
    question sentence. If nothing else would remain, the note is returned unchanged (the bubble then simply
    repeats the question, as for any plain clarifying question)."""
    if not card_question.strip():
        return note
    kept: list[str] = []
    for para in note.split("\n\n"):
        para = para.strip()
        if not para or para == card_question.strip() or _near_duplicate_text(para, card_question):
            continue
        m = _CLOSING_Q.search(para)
        if m:
            q = m.group(0).strip()
            if _SAVE_ADJUST.search(q) or _near_duplicate_text(q, card_question):
                para = para[: -len(q)].rstrip()
        if para:
            kept.append(para)
    return "\n\n".join(kept) if kept else note


def _to_turn_result(state: dict[str, Any]) -> BuilderTurnResult:
    interrupts = state.get("__interrupt__")
    question = None
    similar_suggestions = None
    if interrupts:
        value = interrupts[0].value
        if isinstance(value, dict) and value.get("kind") == "similar_suggestions":
            status = "awaiting_similar_choice"
            similar_suggestions = value.get("suggestions", [])
        elif isinstance(value, dict) and value.get("kind") == "conversational_response":
            # respond_conversationally paused here (see that node's own docstring) — a
            # genuinely conversational, non-mutating reply. Deliberately status="gathering"
            # with question left None (never "awaiting_clarification"): the frontend must
            # render this as a normal assistant chat bubble, not ClarifyingQuestionCard's
            # chip UI — see ChatTurnRead.question in app/schemas/chat.py and
            # ChatWorkspace.tsx's `message.clarifyingQuestion` check.
            status = "gathering"
        else:
            status = "awaiting_clarification"
            question = value
    else:
        status = state.get("status", "gathering")

    messages = state.get("messages") or []
    assistant_note = _visible_turn_notes(messages)
    if question is not None and assistant_note:
        assistant_note = _without_card_question(assistant_note, str(question.get("question") or ""))

    return BuilderTurnResult(
        status=status,
        draft=state.get("draft", {}),
        question=question,
        assistant_note=assistant_note,
        similar_suggestions=similar_suggestions,
    )


def _config_for(
    session_id: str,
    bedrock: BedrockClientProtocol,
    chat_model_id: str | None,
    db: Any | None = None,
    web_search_client: WebSearchClientProtocol | None = None,
    turn_started_at: datetime | None = None,
    jev_client: JevClientProtocol | None = None,
) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": session_id,
            "bedrock_client": bedrock,
            "chat_model_id": chat_model_id,
            # Wired in so check_similarity can run a real pgvector search — optional
            # (None is a valid, silently-skipped configuration; see check_similarity).
            "db_session": db,
            # Wired in so propose_kpis can offer the web_search tool — optional (None
            # means web_search is simply never offered; see propose_kpis).
            "web_search_client": web_search_client,
            # Correlates every live-trace event this turn's nodes emit (see
            # app/ai/turn_events.py) back to this specific turn attempt — the same value
            # written to ChatSession.pending_turn_started_at by app/api/v1/chat.py for the
            # duration of this call. None is a valid, silently-skipped configuration (e.g.
            # seed_session, or this module's own unit tests) — see emit_turn_event.
            "turn_started_at": turn_started_at,
            # Wired in so the three quality-gate checkpoints (see the module comment above
            # MAX_QUALITY_GATE_RETRIES / app/ai/jev_client.py) can rate decisions via Jev.
            # None is a valid, silently-skipped configuration — quality_gate() degrades to
            # "passed" with no client wired in, exactly like an unreachable Jev.
            "jev_client": jev_client,
        }
    }


async def start_session(
    session_id: str,
    first_message: str,
    bedrock: BedrockClientProtocol,
    chat_model_id: str | None = None,
    db: Any | None = None,
    web_search_client: WebSearchClientProtocol | None = None,
    turn_started_at: datetime | None = None,
    jev_client: JevClientProtocol | None = None,
) -> BuilderTurnResult:
    """Kick off a brand-new scorecard-builder session (`POST /chat/sessions`). Pass `db`
    (an `AsyncSession`) to enable the reuse-suggestion similarity check on the initial
    prompt — see `check_similarity`. Pass `web_search_client` to let `propose_kpis`
    research real KPIs/benchmarks for the user's domain — see `app/ai/web_search.py`. Pass
    `jev_client` to enable the three quality-gate checkpoints (see the module comment
    above `MAX_QUALITY_GATE_RETRIES`) — omitted/`None` simply skips gating (treated as
    passed, same as an unreachable Jev; see `app/ai/jev_client.py`). Pass `turn_started_at`
    (see `_config_for`) so this turn's nodes can write live-trace events correlated to it
    — this is in fact the ONLY call site where the multi-agent research fan-out ever
    actually runs (see `research_kpis`'s docstring: `research_done` is set True on every
    other path into the graph), so it's the one that matters most for that feature."""
    manager = get_graph_manager()
    compiled = await manager.get_compiled_graph()
    config = _config_for(session_id, bedrock, chat_model_id, db, web_search_client, turn_started_at, jev_client)
    result_state = await compiled.ainvoke(initial_state(session_id, first_message), config=config)
    return _to_turn_result(result_state)


async def seed_session(
    session_id: str,
    draft: ScorecardDraft,
    context_message: str,
    assistant_note: str,
) -> BuilderTurnResult:
    """Pre-populate a brand-new session's LangGraph state from an existing scorecard
    ("Refine with assistant"), WITHOUT calling the model — so this works (and the user
    immediately sees the loaded draft) even when Bedrock is unavailable.

    Implemented as a checkpoint write (`aupdate_state`) attributed to the terminal
    `confirm` node, so the thread's next step is END: the seeded thread is therefore in
    exactly the same shape as any finished-then-continued session, and the user's first
    real message (via `send_message`) starts a fresh run from START -> gather_info ->
    check_similarity (skipped: `similarity_checked=True`, since the user already picked
    the scorecard to start from) -> research_kpis (skipped: `research_done=True`, same
    rationale — refining an already-KPI'd scorecard doesn't need a fresh domain research
    fan-out) -> propose_kpis, with the seeded draft in state. The `status` written here is
    "gathering", not "confirmed" — nothing has been saved yet."""
    manager = get_graph_manager()
    compiled = await manager.get_compiled_graph()
    config = {"configurable": {"thread_id": session_id}}
    state = initial_state(session_id, context_message)
    state["draft"] = draft.model_dump(mode="json")
    state["similarity_checked"] = True
    state["research_done"] = True
    state["user_msgs_checked"] = 1  # the seeded context message is not a user KPI list
    state["messages"] = [
        {"role": "user", "content": context_message},
        {"role": "assistant", "content": assistant_note},
    ]
    await compiled.aupdate_state(config, state, as_node="confirm")
    return BuilderTurnResult(
        status="gathering",
        draft=state["draft"],
        question=None,
        assistant_note=assistant_note,
    )


async def send_message(
    session_id: str,
    message: str,
    bedrock: BedrockClientProtocol,
    chat_model_id: str | None = None,
    db: Any | None = None,
    web_search_client: WebSearchClientProtocol | None = None,
    turn_started_at: datetime | None = None,
    jev_client: JevClientProtocol | None = None,
) -> BuilderTurnResult:
    """Resume an existing session with a new user message
    (`POST /chat/sessions/{id}/messages`). If the graph is currently paused at
    `ask_clarification` or `suggest_similar`, this resumes exactly there via
    `Command(resume=message)`; otherwise it appends the message as a fresh turn on top
    of the last checkpoint. Pass `turn_started_at` (see `_config_for`/`start_session`) so
    this turn's nodes can write live-trace events correlated to it — a no-op for the
    research fan-out specifically (already `research_done` by this point in every real
    session — see `research_kpis`), but `propose_kpis`'s own master-actor events still
    fire on every turn regardless. Pass `jev_client` to enable the quality-gate
    checkpoints (see `start_session`'s own docstring)."""
    manager = get_graph_manager()
    compiled = await manager.get_compiled_graph()
    config = _config_for(session_id, bedrock, chat_model_id, db, web_search_client, turn_started_at, jev_client)

    snapshot = await compiled.aget_state(config)
    if not snapshot.values:
        # No prior checkpoint for this thread_id at all — treat this call as the start.
        result_state = await compiled.ainvoke(initial_state(session_id, message), config=config)
    elif snapshot.interrupts:
        result_state = await compiled.ainvoke(Command(resume=message), config=config)
    else:
        result_state = await compiled.ainvoke(
            {"session_id": session_id, "messages": [{"role": "user", "content": message}]},
            config=config,
        )
    return _to_turn_result(result_state)


async def get_session_state(session_id: str) -> BuilderTurnResult | None:
    """Inspect current state without advancing the graph (`GET /chat/sessions/{id}`)."""
    manager = get_graph_manager()
    compiled = await manager.get_compiled_graph()
    config = {"configurable": {"thread_id": session_id}}
    snapshot = await compiled.aget_state(config)
    if not snapshot.values:
        interim = _interim_drafts.get(session_id)  # first turn still inside its first node
        if interim:
            return BuilderTurnResult(status="gathering", draft=dict(interim))
        return None
    state = dict(snapshot.values)
    if snapshot.interrupts:
        state["__interrupt__"] = list(snapshot.interrupts)
    state["draft"] = _overlay_interim(session_id, state.get("draft"))
    return _to_turn_result(state)


async def delete_session_checkpoints(session_id: str) -> None:
    """Deletes this session's LangGraph state (`DELETE /chat/sessions/{id}`) — every row
    in the `checkpoints`/`checkpoint_writes`/`checkpoint_blobs` tables (see
    `GraphManager`'s docstring) keyed by this `thread_id`. These tables are owned by
    LangGraph, not the relational `chat_sessions`/`chat_messages` rows (deleted separately,
    with an ordinary FK `ON DELETE CASCADE`, by the API layer) — `AsyncPostgresSaver`'s own
    `adelete_thread` is the documented way to remove them, rather than hand-rolling `DELETE
    ... WHERE thread_id = ...` against tables this module doesn't otherwise own the schema
    of. Safe to call even for a session that never reached a LangGraph checkpoint (e.g. a
    seeded "Refine with assistant" session with no messages sent yet) — deleting a
    thread_id with no rows is a no-op, not an error."""
    _interim_drafts.pop(session_id, None)
    await get_graph_manager().adelete_thread(session_id)
