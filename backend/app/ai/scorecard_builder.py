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
import json
import logging
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.ai.bedrock_client import BedrockClientProtocol, ToolSpec
from app.ai.draft_schema import KpiDraft, ScorecardDraft
from app.ai.jev_client import QUALITY_GATE_THRESHOLD, JevClientProtocol, quality_gate
from app.ai.scoring_formula import validate as validate_scoring_formula
from app.ai.similarity import find_similar_scorecards
from app.ai.turn_events import emit_turn_event
from app.ai.web_search import SearchResult, WebSearchClientProtocol
from app.config import get_settings

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
                    "left to change."
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

_TOOLS = [ASK_CLARIFICATION_TOOL, UPDATE_DRAFT_TOOL, UPDATE_SCORING_FORMULA_TOOL, RESPOND_CONVERSATIONALLY_TOOL]
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
# MAX_RESEARCH_ANGLES x MAX_SEARCH_CALLS_PER_RESEARCH_AGENT — see those constants) — mixing
# the two counters would conflate two independently-reasoned-about budgets for no benefit.
MAX_WEB_SEARCH_CALLS_PER_SESSION = 15


# --- Multi-agent research fan-out (research_kpis node) ----------------------------------
#
# Genuine concurrent fan-out, run ONCE per session (guarded by BuilderState.research_done),
# immediately before the first propose_kpis call: one Bedrock call decides 0-MAX_RESEARCH_
# ANGLES distinct research angles for the user's stated domain/purpose, then that many
# instances of the SAME bounded research-agent worker (`_run_research_agent` below — one
# definition, N concurrent invocations via `asyncio.gather`, mirroring judge.py's
# k-ensemble concurrency idiom) each independently research their one angle with their own
# small web_search budget, and return a structured `ResearchFinding`.
#
# **KPI batching (the fix for "chat only ever proposes 4 KPIs")**: each research agent, once
# it has recorded its finding, makes ONE additional bounded tool call
# (`propose_kpi_batch`/`PROPOSE_KPI_BATCH_TOOL`) proposing its OWN small batch (2-4) of
# fully-specified KPIs — name, weight, AND full 11-level guidelines each — grounded
# entirely in ITS OWN research (it already has the context; no separate downstream call
# re-derives them). Doing this once per agent, in parallel, is the actual mechanism that
# lets the TOTAL KPI count grow with the number of research angles instead of being capped
# by how much one giant end-of-pipeline tool call can productively pack into one JSON
# response — the root cause of the old "always 4 KPIs" behavior. `research_kpis` (below)
# merges every agent's batch (dedup + weight-renormalize + cap — see
# `_merge_research_kpi_batches`) directly into `draft.kpis` BEFORE `propose_kpis` ever
# runs, so `propose_kpis`'s job shifts from "generate KPIs from scratch" to "review this
# already-comprehensive, already-grounded set and reconcile/confirm it with the user" (see
# `propose_kpis`'s own docstring/system prompt update).
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
# existing value needs no adjustment for this feature. The one extra
# `propose_kpi_batch` call each research agent now makes similarly doesn't touch
# MAX_SEARCH_CALLS_PER_RESEARCH_AGENT (it isn't a web_search call) or any per-propose_kpis
# budget — it's one more bounded Bedrock call inside a node that already ran exactly once.
# MAX_LLM_TURNS_PER_HUMAN_TURN itself is ALSO left unchanged even though propose_kpis's job
# changed: reviewing/renormalizing/confirming an already-populated, already-grounded draft
# is if anything LESS work per turn than generating one from scratch, so the existing
# budget (4 consecutive turns) remains generous for that lighter task.
MAX_RESEARCH_ANGLES = 4
MAX_SEARCH_CALLS_PER_RESEARCH_AGENT = 2

# How many KPIs one research agent proposes in its own batch (small and bounded, per KPI
# batch tool call, is exactly what lets the total grow — see the module comment above), and
# the hard ceiling on the TOTAL merged/deduped KPI count `research_kpis` will hand to
# propose_kpis — a safety cap ("can't run away"), deliberately NOT a fixed target (real
# coverage is driven by what research actually surfaced: with 0 angles this is 0, with a
# narrow domain it might land under the cap even with several agents, per the explicit
# "never just a fixed N" instruction).
MAX_KPIS_PER_RESEARCH_BATCH = 4
MAX_TOTAL_MERGED_KPIS = 24

# --- Iterative / multi-round research (bounded) -----------------------------------------
#
# Originally `research_kpis` ran exactly ONE round: decide angles -> fan out -> merge ->
# fall through to propose_kpis, unconditionally. The owner's ask: if the master isn't
# confident that round's research is genuinely sufficient, let it launch ONE MORE bounded
# round of research agents — with NEW/REFINED angles targeting whatever gap it identified
# (see `_assess_research_coverage`), not a repeat of the same searches — before ever
# reaching propose_kpis.
#
# Why the cap is 2, not 3+ (explicit latency/cost reasoning, not a reflexive round number):
# each round is itself 2-4 CONCURRENT research agents, and each agent makes up to
# MAX_SEARCH_CALLS_PER_RESEARCH_AGENT (2) web_search calls PLUS one record_research_finding
# call PLUS one propose_kpi_batch call — i.e. up to ~4 sequential Bedrock/web_search round-
# trips per agent, observed live (per this project's own prior live-testing passes) to take
# 20-90+ seconds for a SINGLE round depending on model/Gateway latency. A second round adds
# one more `_assess_research_coverage` call (cheap, one Bedrock call) plus a second full
# agent fan-out (another 20-90+s) — already a meaningful chunk of a synchronous HTTP
# request's total latency (the chat-turn endpoints block for the whole LangGraph run per
# this module's docstring). A THIRD round would compound that same cost again for
# diminishing returns (by round 3, coverage gaps worth a dedicated concurrent fan-out are
# rare — see `_ASSESS_COVERAGE_SYSTEM_PROMPT`'s explicit "most domains ARE adequately
# covered after one round" framing) while risking a chat turn that feels broken/hung to a
# user waiting on a single HTTP response. 2 is therefore the default; bumping to 3 is a
# one-line change here if the product later decides the extra latency is worth it for
# specific domains, but nothing in this implementation hardcodes "exactly 2" anywhere else.
#
# Interaction with MAX_LLM_TURNS_PER_HUMAN_TURN (the real bug from an earlier pass this
# task explicitly calls out: propose_kpis running out of turns before ever confirming) —
# **this cannot recur from multi-round research**, by construction: every round of the
# loop, the angle-decision call, AND every `_assess_research_coverage` call all happen
# INSIDE this one `research_kpis` node invocation (a plain Python `while` loop below, not
# additional LangGraph nodes/edges) — `research_kpis` still runs at MOST once per session
# (guarded by `research_done`, unchanged) and still never touches `llm_turn_count` (that
# counter only increments on each `propose_kpis` node VISIT — see that counter's own
# docstring). So however many research rounds run (1 or MAX_RESEARCH_ROUNDS), propose_kpis
# always starts its own turn budget completely fresh at MAX_LLM_TURNS_PER_HUMAN_TURN (4) —
# research rounds and the reconciliation/confirmation turns that follow are accounted
# entirely separately, by simply never sharing a counter, rather than needing any new
# bookkeeping to keep them apart.
MAX_RESEARCH_ROUNDS = 2

# --- Quality gate (self-critique layer via Jev/OpenRouter — see app/ai/jev_client.py) ---
#
# Three checkpoints, each wrapping an existing decision point with a bounded
# instruction/answer "does this genuinely serve the request?" rating from Jev:
#   1. _decide_research_angles's plan (research_kpis) — see _decide_research_angles_with_gate.
#   2. Each research agent's synthesized finding (_run_research_agent), right after
#      record_research_finding.
#   3. propose_kpis's final per-visit decision (ask_clarification/update_draft/
#      update_scoring_formula/respond_conversationally).
#
# MAX_QUALITY_GATE_RETRIES=2 (the same "original attempt + up to 2 revisions" shape used
# throughout this module already — see MAX_RESEARCH_ROUNDS/MAX_WEB_SEARCH_CALLS_PER_
# PROPOSE for the same "small, explicit, bounded" pattern): a checkpoint that never clears
# QUALITY_GATE_THRESHOLD (0.75, from jev_client.py) after 2 revision attempts proceeds
# with its BEST-SCORING attempt seen so far rather than hanging the turn or silently
# dropping the result — "never silently hang or crash a turn over this" is the task's own
# explicit requirement. `quality_gate()` itself never raises (see jev_client.py), so an
# unreachable Jev degrades every checkpoint to "passed, no retry" transparently — these
# retry loops only ever engage on a REAL, successfully-obtained low score.
MAX_QUALITY_GATE_RETRIES = 2

DECIDE_RESEARCH_ANGLES_TOOL = ToolSpec(
    name="decide_research_angles",
    description=(
        "Decide which distinct research angles are worth investigating on the web to "
        "ground the KPIs and quantitative thresholds you'll propose for this domain. "
        "Each angle is handed to an independent research agent that runs concurrently "
        "with the others."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "angles": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_RESEARCH_ANGLES,
                "items": {
                    "type": "object",
                    "properties": {
                        "angle": {
                            "type": "string",
                            "description": (
                                "A short label for this research angle, e.g. "
                                "'Industry MTTR/MTTD benchmarks'."
                            ),
                        },
                        "query_focus": {
                            "type": "string",
                            "description": (
                                "A specific, researchable question or focus for this angle "
                                "(not a vague topic) — this is what the research agent is told to investigate."
                            ),
                        },
                    },
                    "required": ["angle", "query_focus"],
                },
            },
        },
        "required": ["angles"],
    },
)

RECORD_RESEARCH_FINDING_TOOL = ToolSpec(
    name="record_research_finding",
    description=(
        "Record your research finding for YOUR SINGLE assigned angle. Call this exactly "
        "once, after using your web_search budget (if you used it)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A concise summary of what you found for this angle.",
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
                "description": "KPI name/rationale candidates this angle's research supports. Empty array if none.",
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
        "Propose YOUR OWN small batch of KPIs (2-4), grounded entirely in the research "
        "you just recorded for your single assigned angle — full name, weight, AND a "
        "full 11-level (0-10) qualitative + quantitative guideline for each. Call this "
        "exactly once, immediately after record_research_finding. Weight each KPI "
        "relative to the OTHERS IN THIS BATCH ONLY (as if they were the only KPIs in the "
        "scorecard) — every other research agent is doing the same for its own batch, and "
        "all batches get merged and re-normalized together afterward, so do not worry "
        "about the overall scorecard's total weight budget here. Return an EMPTY array "
        "only if your research genuinely didn't surface anything KPI-worthy for this "
        "angle."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kpis": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_KPIS_PER_RESEARCH_BATCH,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "A specific, distinct KPI name."},
                        "weight": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 100,
                            "description": (
                                "Relative weight among just this batch's KPIs "
                                "(this batch should sum to ~100)."
                            ),
                        },
                        "guidelines": {
                            "type": "object",
                            "description": (
                                'Keyed by score level "0" through "10" (all 11 required). Each value: '
                                "{qualitative_text (str), quantitative_criteria (JSON OBJECT or null — "
                                "NEVER a plain string; grounded in your research's suggested_thresholds "
                                "where relevant)}."
                            ),
                        },
                    },
                    "required": ["name", "weight", "guidelines"],
                },
            },
        },
        "required": ["kpis"],
    },
)

ASSESS_RESEARCH_COVERAGE_TOOL = ToolSpec(
    name="assess_research_coverage",
    description=(
        "Given the research findings and KPIs gathered so far for this domain, decide "
        "whether research coverage is genuinely SUFFICIENT to proceed to KPI proposal now, "
        "or whether an important, DISTINCT angle/gap remains unresearched. This is the "
        "confidence check between research rounds — be honest: most domains ARE adequately "
        "covered after one round; only report insufficient coverage when you can name a "
        "real, specific, meaningfully different concern the research so far hasn't touched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "sufficient": {
                "type": "boolean",
                "description": (
                    "true if coverage is genuinely adequate to propose a comprehensive, "
                    "well-grounded KPI set now; false only if there's a real, specific gap."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "Brief reasoning for this judgment, grounded in the actual findings/KPIs "
                    "listed above — not a generic statement."
                ),
            },
            "next_angles": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_RESEARCH_ANGLES,
                "items": {
                    "type": "object",
                    "properties": {
                        "angle": {"type": "string", "description": "A short label for this NEW research angle."},
                        "query_focus": {
                            "type": "string",
                            "description": "A specific, researchable question or focus for this angle.",
                        },
                    },
                    "required": ["angle", "query_focus"],
                },
                "description": (
                    "ONLY populate when sufficient=false: 1 or more NEW angles that are "
                    "genuinely DIFFERENT from every angle already investigated (never a "
                    "repeat/rephrasing of one already covered) and that specifically target "
                    "the gap identified in `reasoning`. Leave empty when sufficient=true."
                ),
            },
        },
        "required": ["sufficient", "reasoning", "next_angles"],
    },
)

_DECIDE_ANGLES_SYSTEM_PROMPT = f"""You are the research-planning step for the Quality \
Scorecard System's scorecard-design assistant. Given what the user has said so far about \
the scorecard they want, decide which DISTINCT research angles (0 to {MAX_RESEARCH_ANGLES}) \
are worth investigating on the web to ground the KPIs and quantitative guideline \
thresholds that will be proposed for this domain.

Guidelines:
- Each angle must investigate something meaningfully DIFFERENT — never propose \
near-duplicate angles (e.g. for "vendor security compliance reviews": one angle on \
security standards/frameworks (ISO 27001, NIST, SOC 2), another on regulatory/compliance \
requirements, another on vendor-risk-assessment practice — three genuinely different \
concerns, not the same question worded three ways).
- Choose the NUMBER of angles based on the domain's actual breadth, not a fixed default: \
0 if the domain is narrow/simple enough that no dedicated fan-out is needed (an ordinary \
web_search tool is still available later for ad hoc lookups); 1 if there is exactly one \
clear research need; 2-4 for a domain that genuinely spans multiple distinct concerns.
- Each angle needs a short `angle` label and a specific, researchable `query_focus`.

Base your decision on what the user actually said below — the structured draft fields may \
still be empty/null this early in the conversation (e.g. on the very first message, before \
any clarifying question has been answered), so the user's own words are very likely your \
ONLY signal for the domain right now. Do not treat an empty draft as "not enough \
information" if the user's message already describes a clear domain/purpose.

What the user has said so far (most recent message last):
{{conversation_context}}

Current draft (JSON, may still be mostly empty this early — see above): {{draft_json}}

Call `decide_research_angles` exactly once."""

_RESEARCH_WORKER_SYSTEM_PROMPT_TEMPLATE = """You are ONE independent research agent, one \
of several running concurrently, each investigating a different angle to help ground a \
quality scorecard's KPIs and quantitative thresholds in real, checkable information.

Your assigned angle: "{angle}"
Your specific research focus: "{query_focus}"

Use `web_search` (you have a budget of at most {max_calls} search call(s) this session) to \
find real, current, checkable information — published industry benchmarks, standards, \
frameworks, or regulatory requirements relevant to YOUR angle only. Then call \
`record_research_finding` exactly once. Stay focused on your angle — do not try to cover \
the whole scorecard; other agents are covering the other angles."""

_PROPOSE_KPI_BATCH_SYSTEM_PROMPT_TEMPLATE = """You are the SAME research agent that just \
investigated angle "{angle}" (focus: "{query_focus}") and recorded this finding:

Summary: {summary}
Suggested KPI ideas: {suggested_kpis}
Suggested quantitative thresholds: {suggested_thresholds}
Sources: {sources}

Now propose a small batch (2-4) of fully-specified KPIs for a quality scorecard, grounded \
in what you JUST found above. Each needs: a specific, distinct name; a weight (0-100, \
relative to the other KPIs in THIS batch only — other agents are proposing their own \
batches independently, and everything gets merged and re-normalized afterward); and a \
FULL 11-level (0-10) qualitative + quantitative guideline. Use the suggested_thresholds \
above to ground the quantitative_criteria at each level wherever relevant, rather than \
inventing plausible-sounding numbers. Call `propose_kpi_batch` exactly once."""

_ASSESS_COVERAGE_SYSTEM_PROMPT = f"""You are the research-planning step for the Quality \
Scorecard System's scorecard-design assistant, reviewing the results of research round \
{{rounds_so_far}} to decide whether to propose KPIs now or investigate a genuine gap first. \
THIS is the real confidence check the multi-round research loop is built on — your \
judgment here, grounded in the actual findings below, decides whether a second (bounded) \
round of concurrent research agents runs at all.

What the user has said so far (most recent message last):
{{conversation_context}}

Research angle(s) already investigated so far: {{angles_covered}}

Consolidated findings and KPIs gathered so far:
{{findings_summary}}

Decide honestly: is this coverage genuinely SUFFICIENT to propose a comprehensive, well- \
grounded set of KPIs for this domain now, or is there a real, DISTINCT gap — an important \
angle or concern this domain needs that none of the above touched? Most domains ARE \
adequately covered after one round of focused research — only report `sufficient: false` \
when you can name a SPECIFIC, meaningful, missing concern, never merely "more research is \
always better" or a marginal refinement/rephrasing of an angle already covered above.

If (and only if) insufficient, propose 1 to {MAX_RESEARCH_ANGLES} NEW research angles that \
are genuinely DIFFERENT from every angle listed above and specifically target the gap you \
identified — the whole point of a second round is covering NEW ground, not re-searching \
the same territory.

Call `assess_research_coverage` exactly once."""


@dataclass
class ResearchFinding:
    """Structured output of ONE research-agent invocation (see `_run_research_agent`) —
    what `research_kpis` fans out N of concurrently and consolidates into
    `BuilderState.research_findings`. `degraded=True` marks a finding produced by the
    graceful-failure path (the agent's own Bedrock/web_search calls raised, or the model
    never called `record_research_finding`) rather than a real recorded finding — see
    `research_kpis`'s consolidation step, which drops a degraded finding with no usable
    content instead of feeding empty noise into propose_kpis's context.

    `proposed_kpis`: this agent's own small batch of fully-specified KPI dicts (see
    `PROPOSE_KPI_BATCH_TOOL` — each already has name/weight/guidelines, validated against
    `KpiDraft` by `_run_research_agent` before being placed here), the actual mechanism
    behind Part 1's "KPIs arrive in batches, not one giant end-of-pipeline proposal" fix —
    see the module comment above `MAX_RESEARCH_ANGLES`. Always `[]` for a degraded finding."""

    angle: str
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
    next_angles: list[dict[str, str]]


def _decide_research_angles(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    draft: ScorecardDraft,
    conversation_context: str,
    revision_feedback: str | None = None,
) -> list[dict[str, str]]:
    """One synchronous Bedrock call (run via `asyncio.to_thread` by the caller) deciding
    how many/which research angles this domain warrants. Never raises past this function's
    own belt-and-suspenders try/except at the call site in `research_kpis` — a malformed or
    missing tool response here is treated as "no angles decided", not a crash.

    `conversation_context` (the user's own messages so far — see `research_kpis`) is the
    critical signal on a session's very first turn, when `draft` is still entirely empty:
    research_kpis runs BEFORE propose_kpis has ever had a chance to populate the structured
    draft fields, so the draft alone would tell this call nothing about the domain yet.

    `revision_feedback` (set only on a retry — see `_decide_research_angles_with_gate`,
    quality-gate checkpoint 1) appends Jev's below-threshold verdict to the system prompt
    so a revised plan is a genuine reconsideration, not a blind re-roll of the same call."""
    system_prompt = _DECIDE_ANGLES_SYSTEM_PROMPT.format(
        draft_json=json.dumps(draft.model_dump(mode="json")),
        conversation_context=conversation_context or "(nothing yet)",
    )
    if revision_feedback:
        system_prompt += f"\n\n{revision_feedback}"
    result = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": "Decide the research angles for this scorecard."}]}],
        system=system_prompt,
        tools=[DECIDE_RESEARCH_ANGLES_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_name != "decide_research_angles":
        return []
    raw_angles = (result.tool_input or {}).get("angles") or []
    cleaned: list[dict[str, str]] = []
    for raw in raw_angles[:MAX_RESEARCH_ANGLES]:
        if not isinstance(raw, dict):
            continue
        angle = str(raw.get("angle") or "").strip()
        query_focus = str(raw.get("query_focus") or "").strip()
        if angle and query_focus:
            cleaned.append({"angle": angle, "query_focus": query_focus})
    return cleaned


def _angles_to_gate_text(angles: list[dict[str, str]]) -> str:
    """Renders a decided research-angle plan as plain text for the quality gate's `answer`
    (see `_decide_research_angles_with_gate`) — an empty plan is itself a valid, ratable
    answer ("the model judged no dedicated research was needed"), not a special case."""
    if not angles:
        return (
            "(No dedicated research angles were planned — the domain was judged "
            "narrow/simple enough that no research fan-out is needed.)"
        )
    return "Planned research angles:\n" + "\n".join(
        f'- "{a["angle"]}" — focus: {a["query_focus"]}' for a in angles
    )


async def _decide_research_angles_with_gate(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    draft: ScorecardDraft,
    conversation_context: str,
    jev_client: JevClientProtocol | None,
    session_id: str,
    turn_started_at: datetime | None,
) -> list[dict[str, str]]:
    """Quality-gate checkpoint 1 (see the module comment above `MAX_QUALITY_GATE_RETRIES`):
    wraps `_decide_research_angles` with a Jev rating of how well the decided plan serves
    the user's actual request (instruction=`conversation_context`, answer=the plan
    itself). Below `QUALITY_GATE_THRESHOLD`, the master revises its plan — bounded at
    `MAX_QUALITY_GATE_RETRIES` revisions — then proceeds with the best-scoring attempt
    seen (never hangs, never silently drops a low-scoring plan)."""
    instruction = conversation_context or "(nothing yet)"
    best_angles: list[dict[str, str]] = []
    best_score = -1.0
    revision_feedback: str | None = None

    for attempt in range(MAX_QUALITY_GATE_RETRIES + 1):
        angles = await asyncio.to_thread(
            _decide_research_angles, bedrock, model_id, draft, conversation_context, revision_feedback
        )
        gate = await quality_gate(jev_client, instruction=instruction, answer=_angles_to_gate_text(angles))

        if gate.degraded:
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate",
                "Research plan quality check: Jev was unreachable — gate treated as passed "
                "(graceful degradation).",
            )
            return angles  # Jev unreachable — gate passed by policy; no point retrying.
        assert gate.score is not None  # guaranteed whenever degraded=False
        if gate.score > best_score:
            best_angles, best_score = angles, gate.score

        if gate.passed:
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate",
                f"Research plan quality check scored {gate.score:.2f} (>= {QUALITY_GATE_THRESHOLD}) — "
                + ("passed after revision." if attempt > 0 else "passed."),
            )
            return angles

        if attempt < MAX_QUALITY_GATE_RETRIES:
            await emit_turn_event(
                session_id, turn_started_at, "master", "quality_gate_retry",
                f"Quality check scored {gate.score:.2f} (below {QUALITY_GATE_THRESHOLD}) — "
                "revising the research plan…",
            )
            revision_feedback = (
                f"An automated quality check scored your previous research-angle plan "
                f"{gate.score:.2f}/1.0 (threshold {QUALITY_GATE_THRESHOLD}) for how well it "
                "serves the user's actual request. Reconsider whether your chosen angles "
                "genuinely target what the user asked for, and revise the angles and/or "
                "query_focus wording to better match their request before deciding again."
            )

    await emit_turn_event(
        session_id, turn_started_at, "master", "quality_gate",
        f"Research plan quality check still below {QUALITY_GATE_THRESHOLD} after "
        f"{MAX_QUALITY_GATE_RETRIES} revision(s) (best score {best_score:.2f}) — "
        "proceeding with the best attempt.",
    )
    return best_angles


def _clean_angle_items(raw_angles: list[Any]) -> list[dict[str, str]]:
    """Shared cleaning logic for a list of `{angle, query_focus}` dicts, however they were
    produced (`decide_research_angles`'s `angles` or `assess_research_coverage`'s
    `next_angles` — both use the identical shape) — factored out of `_decide_research_angles`
    so `_assess_research_coverage` doesn't duplicate the same defensive parsing."""
    cleaned: list[dict[str, str]] = []
    for raw in raw_angles[:MAX_RESEARCH_ANGLES]:
        if not isinstance(raw, dict):
            continue
        angle = str(raw.get("angle") or "").strip()
        query_focus = str(raw.get("query_focus") or "").strip()
        if angle and query_focus:
            cleaned.append({"angle": angle, "query_focus": query_focus})
    return cleaned


def _assess_research_coverage(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    draft: ScorecardDraft,
    conversation_context: str,
    findings: list[ResearchFinding],
    angles_covered: list[dict[str, str]],
    rounds_so_far: int,
) -> CoverageAssessment:
    """One synchronous Bedrock call (run via `asyncio.to_thread` by the caller, mirrors
    `_decide_research_angles`'s own shape) — the confidence-check mechanism behind the
    multi-round research loop (see `research_kpis` and `MAX_RESEARCH_ROUNDS`'s own
    docstring). A REAL judgment: the model is shown the actual consolidated findings and
    the KPIs merged so far and asked to decide, grounded in that real content, whether a
    genuine gap remains — never a hardcoded heuristic, a coin flip, or an always-true/
    always-false stub (per the task's explicit requirement).

    Fails SAFE: any malformed tool response, wrong tool name, or exception is treated as
    `sufficient=True` (see `CoverageAssessment`'s own docstring) — so a Bedrock hiccup on
    this one call degrades to "proceed with what we have", exactly what happened before
    this feature existed, never an infinite or stuck research loop."""
    findings_summary = _format_research_findings_for_prompt([asdict(f) for f in findings]) or "(no findings yet)"
    current_kpi_names = ", ".join(kpi.name for kpi in draft.kpis) or "(none yet)"
    angles_text = (
        "; ".join(f'"{a["angle"]}" (focus: {a["query_focus"]})' for a in angles_covered) or "(none)"
    )
    system_prompt = _ASSESS_COVERAGE_SYSTEM_PROMPT.format(
        rounds_so_far=rounds_so_far,
        conversation_context=conversation_context or "(nothing yet)",
        angles_covered=angles_text,
        findings_summary=f"{findings_summary}\n\nKPIs merged so far: {current_kpi_names}",
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
        return CoverageAssessment(sufficient=True, reasoning="(no usable assessment returned)", next_angles=[])

    data = result.tool_input or {}
    next_angles = _clean_angle_items(data.get("next_angles") or [])
    # sufficient=True OR no usable next_angles both mean "stop" — an "insufficient" verdict
    # with nothing concrete to research next is functionally the same as "sufficient" here.
    sufficient = bool(data.get("sufficient", True)) or not next_angles
    return CoverageAssessment(
        sufficient=sufficient, reasoning=str(data.get("reasoning") or ""), next_angles=next_angles
    )


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
    angle: str,
    query_focus: str,
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
) -> ResearchFinding:
    """THE single research-agent definition (instantiated/invoked N times concurrently by
    `research_kpis` via `asyncio.gather`, never duplicated in code) — a bounded ReAct-style
    worker: LLM + web_search, capped at MAX_SEARCH_CALLS_PER_RESEARCH_AGENT search calls
    then forced to close with `record_research_finding`, mirroring propose_kpis's own
    bounded web_search loop (see MAX_WEB_SEARCH_CALLS_PER_PROPOSE).

    `actor` (e.g. `"research_agent_2"`, assigned index-based by the caller WITHIN its
    round — see `research_kpis`) is this specific concurrent invocation's stable identifier
    for the live-trace event log (see `app/ai/turn_events.py`): every event this worker
    emits is tagged with it, so a reader can tell which of the N concurrently-running
    agents produced which event even though they interleave in `created_at` order.
    `round_num` (default 1) additionally tags every event with which research ROUND this
    invocation belongs to (see `MAX_RESEARCH_ROUNDS`) — `actor` alone is ambiguous across
    rounds since it resets to `research_agent_1`, `research_agent_2`, ... at the start of
    every round; `round_num` is what lets a reader (or `TurnTraceCard.tsx`) tell round 2's
    `research_agent_1` apart from round 1's.

    Contract: NEVER raises — mirrors web_search.py's own "return [], never raise" contract
    (see that module's docstring) one level up. Any failure anywhere in this worker (a
    Bedrock call, a malformed response, an unexpected web_search exception) is caught here
    and turned into a thin `degraded=True` finding, so one failing angle can never crash
    the whole turn or the other concurrently-running agents (which are independent asyncio
    tasks and are completely unaffected by this one's exception either way).

    `jev_client`/`conversation_context` power quality-gate checkpoint 2 (see the module
    comment above `MAX_QUALITY_GATE_RETRIES`): once `record_research_finding` produces a
    finding, Jev rates how well it matches this agent's assigned angle/focus
    (instruction=`conversation_context` + angle/focus, answer=the finding). Below
    threshold, the agent researches further and re-synthesizes — bounded at
    `MAX_QUALITY_GATE_RETRIES` attempts, sharing the SAME `MAX_SEARCH_CALLS_PER_RESEARCH_
    AGENT` web_search budget across every attempt (not reset per retry) — then returns its
    best-scoring finding rather than hanging or dropping the angle."""
    try:
        await emit_turn_event(
            session_id, turn_started_at, actor, "started", f'Researching "{angle}": {query_focus}', round=round_num
        )
        local_messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [{"text": f"Research angle: {angle}\nFocus: {query_focus}"}],
            }
        ]
        search_calls_made = 0
        finding: ResearchFinding | None = None
        last_result = None
        best_finding: ResearchFinding | None = None
        best_score = -1.0
        revision_feedback: str | None = None

        # Outer loop = quality-gate checkpoint 2's bounded retry (see the module comment
        # above MAX_QUALITY_GATE_RETRIES); inner `while True` = the UNCHANGED bounded
        # search-then-record loop, reused as-is on every gate attempt (search budget is
        # shared/not reset across attempts — see this function's own docstring).
        for gate_attempt in range(MAX_QUALITY_GATE_RETRIES + 1):
            result = None
            while True:
                budget_left = search_calls_made < MAX_SEARCH_CALLS_PER_RESEARCH_AGENT
                tools = (
                    [WEB_SEARCH_TOOL, RECORD_RESEARCH_FINDING_TOOL]
                    if budget_left
                    else [RECORD_RESEARCH_FINDING_TOOL]
                )
                system_prompt = _RESEARCH_WORKER_SYSTEM_PROMPT_TEMPLATE.format(
                    angle=angle, query_focus=query_focus, max_calls=MAX_SEARCH_CALLS_PER_RESEARCH_AGENT
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
                        f'Synthesizing findings for "{angle}"…',
                        round=round_num,
                    )
                if revision_feedback:
                    # Only set on a quality-gate retry (gate_attempt > 0) — see below.
                    system_prompt += f"\n\n{revision_feedback}"

                result = await asyncio.to_thread(
                    bedrock.converse,
                    messages=local_messages,
                    system=system_prompt,
                    tools=tools,
                    force_tool_use=True,
                    model_id=model_id,
                )

                if result.is_tool_use and result.tool_name == "web_search" and budget_left:
                    query = str((result.tool_input or {}).get("query") or "").strip()
                    search_calls_made += 1
                    await emit_turn_event(
                        session_id, turn_started_at, actor, "searching", f'Searching: "{query}"', round=round_num
                    )
                    logger.info(
                        "research agent angle=%r: web_search (%d/%d) query=%r",
                        angle,
                        search_calls_made,
                        MAX_SEARCH_CALLS_PER_RESEARCH_AGENT,
                        query,
                    )
                    try:
                        results = await web_search_client.search(query) if query else []
                    except Exception:  # noqa: BLE001 — see web_search.py's own "never raise" contract
                        logger.warning(
                            "research agent angle=%r: web_search raised unexpectedly; treating as no results.",
                            angle,
                            exc_info=True,
                        )
                        results = []
                    await emit_turn_event(
                        session_id, turn_started_at, actor, "search_result",
                        f'Found {len(results)} result(s) for "{query}"',
                        round=round_num,
                    )
                    logger.info(
                        "research agent angle=%r: web_search query=%r -> %d result(s)%s",
                        angle,
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
                angle=angle,
                summary=str(data.get("summary") or ""),
                suggested_kpis=[k for k in (data.get("suggested_kpis") or []) if isinstance(k, dict)],
                suggested_thresholds=[
                    t for t in (data.get("suggested_thresholds") or []) if isinstance(t, dict)
                ],
                sources=[s for s in (data.get("sources") or []) if isinstance(s, dict)],
            )

            # --- Quality-gate checkpoint 2 (see the module comment above
            # MAX_QUALITY_GATE_RETRIES): rate how well this finding matches what this
            # agent was actually assigned to research.
            gate_instruction = (
                f"User's original request: {conversation_context or '(nothing yet)'}\n"
                f'This research agent\'s assigned angle: "{angle}"\n'
                f'Specific research focus: "{query_focus}"'
            )
            gate = await quality_gate(
                jev_client, instruction=gate_instruction, answer=_finding_to_gate_text(candidate)
            )

            if gate.degraded:
                await emit_turn_event(
                    session_id, turn_started_at, actor, "quality_gate",
                    f'"{angle}": finding quality check — Jev was unreachable — gate treated as '
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
                    f'"{angle}": finding quality check scored {gate.score:.2f} '
                    f"(>= {QUALITY_GATE_THRESHOLD}) — "
                    + ("passed after revision." if gate_attempt > 0 else "passed."),
                    round=round_num,
                )
                break

            if gate_attempt < MAX_QUALITY_GATE_RETRIES:
                await emit_turn_event(
                    session_id, turn_started_at, actor, "quality_gate_retry",
                    f'"{angle}": finding quality check scored {gate.score:.2f} '
                    f"(below {QUALITY_GATE_THRESHOLD}) — researching further…",
                    round=round_num,
                )
                local_messages.append(
                    {"role": "assistant", "content": [{"text": f"[recorded finding] {json.dumps(data)}"}]}
                )
                revision_feedback = (
                    f"An automated quality check scored your recorded finding {gate.score:.2f}/1.0 "
                    f"(threshold {QUALITY_GATE_THRESHOLD}) for how well it matches your assigned angle "
                    f'"{angle}" (focus: "{query_focus}"). Research further (use any remaining '
                    "web_search budget) and call record_research_finding again with an improved, "
                    "more relevant finding."
                )
                local_messages.append({"role": "user", "content": [{"text": revision_feedback}]})
                continue

            # Bounded cap reached and still below threshold — proceed with the best-scoring
            # attempt seen across every gate_attempt (never hang, never silently drop it).
            finding = best_finding if best_finding is not None else candidate
            await emit_turn_event(
                session_id, turn_started_at, actor, "quality_gate",
                f'"{angle}": finding quality check still below {QUALITY_GATE_THRESHOLD} after '
                f"{MAX_QUALITY_GATE_RETRIES} revision(s) (best score {best_score:.2f}) — "
                "using the best attempt.",
                round=round_num,
            )
            break

        if finding is not None:
            # --- KPI batch proposal (Part 1 fix — see the module comment above
            # MAX_RESEARCH_ANGLES): ONE additional bounded tool call, grounded in the
            # finding this same agent just recorded (post-quality-gate), proposing this
            # agent's own small batch of fully-specified KPIs. Never lets a failure here
            # lose the finding itself (finding.proposed_kpis simply stays [] — the finding
            # above is already fully formed and returned regardless).
            await emit_turn_event(
                session_id, turn_started_at, actor, "proposing_kpis", f'Proposing KPIs for "{angle}"…',
                round=round_num,
            )
            try:
                batch_system_prompt = _PROPOSE_KPI_BATCH_SYSTEM_PROMPT_TEMPLATE.format(
                    angle=angle,
                    query_focus=query_focus,
                    summary=finding.summary or "(none)",
                    suggested_kpis=json.dumps(finding.suggested_kpis),
                    suggested_thresholds=json.dumps(finding.suggested_thresholds),
                    sources=json.dumps(finding.sources),
                )
                batch_result = await asyncio.to_thread(
                    bedrock.converse,
                    messages=[{"role": "user", "content": [{"text": "Propose your KPI batch now."}]}],
                    system=batch_system_prompt,
                    tools=[PROPOSE_KPI_BATCH_TOOL],
                    force_tool_use=True,
                    model_id=model_id,
                )
                if batch_result.is_tool_use and batch_result.tool_name == "propose_kpi_batch":
                    raw_items = (batch_result.tool_input or {}).get("kpis") or []
                    finding.proposed_kpis = _validate_kpi_batch_items(raw_items)
                else:
                    logger.warning(
                        "research agent angle=%r: propose_kpi_batch did not return a usable tool call "
                        "(stop_reason=%r).",
                        angle,
                        getattr(batch_result, "stop_reason", None),
                    )
            except Exception:  # noqa: BLE001 — a failed KPI-batch call must never lose the finding itself
                logger.warning("research agent angle=%r: propose_kpi_batch call failed.", angle, exc_info=True)

            if finding.proposed_kpis:
                names = ", ".join(k["name"] for k in finding.proposed_kpis)
                await emit_turn_event(
                    session_id, turn_started_at, actor, "proposed_kpis",
                    f'Proposed {len(finding.proposed_kpis)} KPI(s) for "{angle}": {names}',
                    round=round_num,
                )
            else:
                await emit_turn_event(
                    session_id, turn_started_at, actor, "proposed_kpis",
                    f'"{angle}": no KPIs proposed from this angle.',
                    round=round_num,
                )

            await emit_turn_event(
                session_id, turn_started_at, actor, "completed",
                f'Finished "{angle}": {len(finding.proposed_kpis)} KPI(s) proposed, '
                f"{len(finding.suggested_thresholds)} threshold(s), {len(finding.sources)} source(s).",
                round=round_num,
            )
            return finding

        logger.warning(
            "research agent angle=%r: model did not call record_research_finding "
            "(stop_reason=%r); returning a degraded finding.",
            angle,
            getattr(last_result, "stop_reason", None),
        )
        await emit_turn_event(
            session_id, turn_started_at, actor, "error",
            f'"{angle}": model did not produce a usable finding — skipping this angle.',
            round=round_num,
        )
        return ResearchFinding(angle=angle, summary=(last_result.text if last_result else "") or "", degraded=True)
    except Exception:  # noqa: BLE001 — this worker's whole-agent "never raise" contract; see docstring
        logger.warning("research agent angle=%r failed entirely; returning a degraded finding.", angle, exc_info=True)
        await emit_turn_event(
            session_id, turn_started_at, actor, "error", f'"{angle}": research agent failed — skipping this angle.',
            round=round_num,
        )
        return ResearchFinding(angle=angle, degraded=True)


def _format_research_findings_for_prompt(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return ""
    lines = [
        f"Grounding research from {len(findings)} independently-researched angle(s) — weave "
        "these real findings, and ESPECIALLY their suggested_thresholds/sources, into your "
        "KPI proposal and quantitative_criteria instead of inventing plausible-sounding numbers:"
    ]
    for f in findings:
        lines.append(f"\n## Angle: {f.get('angle', '(unknown)')}")
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


def _validate_kpi_batch_items(raw_items: list[Any]) -> list[dict[str, Any]]:
    """Validates each raw `propose_kpi_batch` item against `KpiDraft` (weight bounds,
    guideline score-level keys, etc — the same schema `update_draft` patches are validated
    against) — an invalid item is dropped and logged, never allowed to reach the merge step
    or corrupt draft state. Every item is forced flat (`level=1`, `parent_name=None`): a
    research agent only ever proposes independent, non-hierarchical KPI candidates (it has
    no visibility into what the OTHER concurrently-running agents are proposing, so it
    cannot meaningfully nest under a sibling it doesn't know exists) — restructuring into a
    hierarchy, if the user wants one, is left to propose_kpis's reconciliation pass.

    Guideline values are repaired via `_normalize_kpi_guidelines` BEFORE validation (see
    that function's own docstring for the real, observed failure mode it recovers from) —
    a candidate is only ever dropped now for a GENUINE validation failure (a real missing
    field, an out-of-range weight, an invalid score-level key, ...), not merely because the
    model nested a qualitative description one level too shallow."""
    validated: list[dict[str, Any]] = []
    for raw in raw_items[:MAX_KPIS_PER_RESEARCH_BATCH]:
        if not isinstance(raw, dict):
            continue
        candidate = _normalize_kpi_guidelines({**raw, "level": 1, "parent_name": None})
        try:
            kpi = KpiDraft.model_validate(candidate)
        except ValidationError:
            logger.warning("propose_kpi_batch: dropping an invalid KPI item %r", raw, exc_info=True)
            continue
        validated.append(kpi.model_dump(mode="json"))
    return validated


def _normalize_kpi_name_for_dedup(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _dedupe_kpi_batch_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simple name/concept-similarity dedup across different agents' batches (e.g. two
    agents both proposing a "Response Time" KPI) — doesn't need to be fancy per this
    feature's own spec. A pair is treated as a duplicate when their normalized names are
    either a close fuzzy match (`difflib.SequenceMatcher` ratio >= 0.82) or one is a
    substring of the other AND both are long enough (>= 8 normalized chars) for that
    substring relationship to be meaningful rather than a coincidence of short names."""
    kept: list[dict[str, Any]] = []
    kept_norms: list[str] = []
    for item in items:
        norm = _normalize_kpi_name_for_dedup(str(item.get("name") or ""))
        if not norm:
            continue
        is_dup = False
        for existing in kept_norms:
            ratio = difflib.SequenceMatcher(None, norm, existing).ratio()
            substring_dup = len(norm) >= 8 and len(existing) >= 8 and (norm in existing or existing in norm)
            if ratio >= 0.82 or substring_dup:
                is_dup = True
                break
        if is_dup:
            continue
        kept.append(item)
        kept_norms.append(norm)
    return kept


def _normalize_weights_to_100(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each research agent weighted its own batch relative to itself only (see
    `PROPOSE_KPI_BATCH_TOOL`'s description) — after merging N agents' batches the pool's
    total is roughly N x 100, not 100. Proportionally rescale every `included_in_scoring`
    KPI's weight so the merged set already satisfies the DB's sibling-sum-to-100 rule
    (mirrors `redistribute_weight`'s "remainder on the largest" rounding-drift fix in
    `frontend/lib/kpi-tree.ts`) — this is a mechanical starting point, not a qualitative
    judgment; `propose_kpis`'s reconciliation pass can still adjust it based on the user's
    actual priorities. KPIs with `included_in_scoring=False` are left untouched (they are
    not part of the sum-to-100 group at all — see migration
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


def _merge_research_kpi_batches(findings: list[ResearchFinding]) -> list[dict[str, Any]]:
    """The merge point (Part 1 fix — see the module comment above MAX_RESEARCH_ANGLES):
    round-robin interleaves every agent's own KPI batch (so, if the pool ends up over the
    cap, no single agent's whole batch crowds out the others), dedupes near-identical
    concepts across agents, caps the total at MAX_TOTAL_MERGED_KPIS (a safety ceiling, NOT
    a fixed target — real coverage is whatever the research actually surfaced), then
    renormalizes weights to sum to 100. Returns `[]` (a no-op merge) if no agent proposed
    anything usable."""
    batches = [f.proposed_kpis for f in findings if f.proposed_kpis]
    interleaved: list[dict[str, Any]] = []
    i = 0
    while any(i < len(batch) for batch in batches):
        for batch in batches:
            if i < len(batch):
                interleaved.append(batch[i])
        i += 1

    deduped = _dedupe_kpi_batch_items(interleaved)
    capped = deduped[:MAX_TOTAL_MERGED_KPIS]
    return _normalize_weights_to_100(capped)


def _web_search_usable(client: WebSearchClientProtocol | None) -> bool:
    """Whether it's worth spending the extra `decide_research_angles` Bedrock call at all.
    Unlike propose_kpis's own ad hoc `web_search` tool offering (unchanged — still offered
    whenever `client is not None`, since a misconfigured real client's own `.search()`
    gracefully returns `[]` per its documented contract, and offering it costs nothing
    extra there), `research_kpis` unconditionally spends one whole Bedrock call up front
    just to decide angles — not worth it if search can't possibly return anything.
    `AgentCoreWebSearchClient` exposes `is_configured` for exactly this; a client that
    doesn't expose it (e.g. `FakeWebSearchClient`/`FakeBedrockClient` in tests, or any
    other `WebSearchClientProtocol` implementation) is assumed usable."""
    if client is None:
        return False
    return getattr(client, "is_configured", True)


async def research_kpis(state: BuilderState, config: RunnableConfig) -> dict[str, Any]:
    """The master/orchestrator step: given the user's stated domain/purpose so far, decides
    (one Bedrock call, see `_decide_research_angles`) how many distinct research angles this
    domain warrants, then runs that many instances of the ONE `_run_research_agent` worker
    CONCURRENTLY via `asyncio.gather` (genuine parallel execution — every angle's Bedrock +
    web_search calls are in flight together, not one after another), and consolidates the
    structured findings into state for `propose_kpis` to ground its proposal in.

    **Iterative / multi-round research**: after round 1's fan-out and merge, if
    `MAX_RESEARCH_ROUNDS` (2) hasn't been reached yet, the master runs ONE real confidence
    check (`_assess_research_coverage` — a genuine Bedrock judgment grounded in the actual
    consolidated findings/KPIs gathered so far, never a hardcoded heuristic) asking whether
    coverage is genuinely sufficient or whether a real, distinct gap remains. If the model
    identifies a real gap AND proposes new/refined angles for it, a SECOND bounded round of
    the same concurrent fan-out runs against those NEW angles (given full context on what
    round 1 already covered, so it targets an actual gap rather than re-searching the same
    ground — see `_ASSESS_COVERAGE_SYSTEM_PROMPT`). This whole multi-round loop is a plain
    Python `while` loop INSIDE this one node invocation, not additional LangGraph nodes —
    see `MAX_RESEARCH_ROUNDS`'s own docstring for why that's what keeps this feature from
    ever affecting `MAX_LLM_TURNS_PER_HUMAN_TURN`.

    Runs at most ONCE per session (guarded by `research_done`, set on every path out of this
    node, unchanged by this feature) — see `MAX_RESEARCH_ROUNDS`'s own docstring for why
    this (now potentially multi-round) node still can't affect MAX_LLM_TURNS_PER_HUMAN_TURN.
    A no-op (research_done=True, no findings) when no `web_search_client` is wired in
    (mirrors propose_kpis's own gating), or when round 1's angle decision itself fails/
    returns nothing — propose_kpis then simply proceeds exactly as it did before this
    feature existed (its own ad hoc web_search + the model's own knowledge), which is the
    graceful-degradation path required when Bedrock/web_search is unavailable."""
    if state.get("research_done"):
        return {}

    configurable = config.get("configurable", {})
    bedrock: BedrockClientProtocol | None = configurable.get("bedrock_client")
    model_id: str | None = configurable.get("chat_model_id")
    web_search_client: WebSearchClientProtocol | None = configurable.get("web_search_client")
    jev_client: JevClientProtocol | None = configurable.get("jev_client")
    session_id: str = state["session_id"]
    turn_started_at: datetime | None = configurable.get("turn_started_at")

    if bedrock is None or not _web_search_usable(web_search_client):
        return {"research_done": True}

    draft = ScorecardDraft.model_validate(state["draft"])
    conversation_context = _conversation_context_text(state.get("messages") or [])

    await emit_turn_event(
        session_id, turn_started_at, "master", "started",
        "Reviewing what you've said so far to plan research angles…",
    )

    try:
        angles = await _decide_research_angles_with_gate(
            bedrock, model_id, draft, conversation_context, jev_client, session_id, turn_started_at
        )
    except Exception:  # noqa: BLE001 — angle decision failing must not break the turn either
        logger.warning("research_kpis: failed to decide research angles; skipping fan-out.", exc_info=True)
        await emit_turn_event(
            session_id, turn_started_at, "master", "error",
            "Could not plan research angles — proceeding without dedicated research.",
        )
        return {"research_done": True}

    if not angles:
        logger.info("research_kpis: model decided no dedicated research angles were needed.")
        await emit_turn_event(
            session_id, turn_started_at, "master", "deciding_angles",
            "Determined this domain doesn't need a dedicated research fan-out.",
        )
        return {"research_done": True}

    # --- Multi-round loop (bounded by MAX_RESEARCH_ROUNDS — see that constant's own
    # docstring for the full latency/cost/MAX_LLM_TURNS_PER_HUMAN_TURN reasoning). Round 1
    # always runs with the angles `_decide_research_angles` just chose above; every
    # subsequent round (if any) runs with the NEW angles `_assess_research_coverage`
    # proposed for a genuine identified gap. `all_findings`/`all_angles_covered` accumulate
    # across every round so far, so the merge step (`_merge_research_kpi_batches`) and the
    # confidence check both always see the FULL picture, not just the latest round.
    all_findings: list[ResearchFinding] = []
    all_angles_covered: list[dict[str, str]] = []
    merged_kpis: list[dict[str, Any]] = []
    updated_draft_dict = draft.model_dump(mode="json")
    total_proposed_before_merge = 0
    round_num = 1

    while True:
        logger.info(
            "research_kpis: round %d dispatching %d research agent(s) concurrently: %s",
            round_num, len(angles), angles,
        )
        if round_num == 1:
            await emit_turn_event(
                session_id, turn_started_at, "master", "deciding_angles",
                f"Decided on {len(angles)} research angle(s): "
                + "; ".join(f'"{a["angle"]}"' for a in angles),
                round=round_num,
            )
        else:
            # Distinct wording (never identical to round 1's message) so the live trace
            # UI/logs make clear this round exists BECAUSE round 1 wasn't judged
            # sufficient — see `_assess_research_coverage`'s reasoning, echoed here too.
            await emit_turn_event(
                session_id, turn_started_at, "master", "deciding_angles",
                f"Round {round_num}: investigating {len(angles)} new angle(s) to address a "
                "gap round 1 didn't cover: " + "; ".join(f'"{a["angle"]}"' for a in angles),
                round=round_num,
            )

        raw_findings = await asyncio.gather(
            *(
                _run_research_agent(
                    a["angle"],
                    a["query_focus"],
                    bedrock,
                    web_search_client,
                    model_id,
                    session_id=session_id,
                    turn_started_at=turn_started_at,
                    actor=f"research_agent_{i + 1}",
                    round_num=round_num,
                    jev_client=jev_client,
                    conversation_context=conversation_context,
                )
                for i, a in enumerate(angles)
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
            round_num, len(usable), len(angles), len(round_findings) - len(usable),
        )

        all_findings.extend(usable)
        all_angles_covered.extend(angles)
        total_proposed_before_merge += sum(len(f.proposed_kpis) for f in usable)

        # --- Merge point (Part 1 fix) — see _merge_research_kpi_batches's own docstring.
        # Re-run over ALL findings accumulated so far (not just this round's), so a
        # round-2 agent's near-duplicate of a round-1 KPI is caught by the same dedup pass
        # and the whole pool is renormalized together — never two independently-normalized
        # pools bolted together.
        candidate_kpis = _merge_research_kpi_batches(all_findings)
        if candidate_kpis:
            candidate_draft_dict = {**draft.model_dump(mode="json"), "kpis": candidate_kpis}
            try:
                ScorecardDraft.model_validate(candidate_draft_dict)  # sanity check before committing to state
                updated_draft_dict = candidate_draft_dict
                merged_kpis = candidate_kpis
            except ValidationError:
                logger.warning(
                    "research_kpis: round %d merged KPI batch failed ScorecardDraft validation; "
                    "keeping the last known-good merge.",
                    round_num, exc_info=True,
                )

        logger.info(
            "research_kpis: round %d — merged %d proposed KPI(s) from %d agent batch(es) so far into "
            "%d deduped/capped KPI(s): %s",
            round_num, total_proposed_before_merge, len(all_findings), len(merged_kpis),
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
                    all_angles_covered,
                    round_num,
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
            and bool(assessment.next_angles)
        )

        if will_continue:
            assert assessment is not None  # for mypy/readability — guarded by will_continue above
            await emit_turn_event(
                session_id, turn_started_at, "master", "completed",
                f"Round {round_num} complete — {len(usable)}/{len(angles)} finding(s), "
                f"{len(merged_kpis)} KPI(s) merged so far. Not yet sufficient: "
                f"{assessment.reasoning or 'a real gap remains'} — starting round {round_num + 1}.",
                round=round_num,
            )
            angles = assessment.next_angles
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
            f"{len(all_findings)} finding(s) across {len(all_angles_covered)} angle(s); merged "
            f"{total_proposed_before_merge} proposed KPI(s) into {len(merged_kpis)} deduped KPI(s) for review.",
            round=round_num,
        )
        break

    note = (
        f"[research] investigated {len(all_angles_covered)} angle(s) across {round_num} round(s): "
        + ", ".join(a["angle"] for a in all_angles_covered)
        + (
            f"; merged {len(merged_kpis)} KPI(s) from per-agent batches: "
            + ", ".join(k["name"] for k in merged_kpis)
            if merged_kpis
            else ""
        )
    )
    return {
        "research_done": True,
        "research_findings": [asdict(f) for f in all_findings] if all_findings else None,
        "draft": updated_draft_dict,
        "messages": [{"role": "assistant", "content": note}],
    }


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
# rounds internally (angle decisions, confidence checks, and every round's agent fan-out
# all happen inside that one node invocation, still before propose_kpis's first visit) —
# see MAX_RESEARCH_ROUNDS's own docstring for the full reasoning on why research rounds and
# propose_kpis's reconciliation/confirmation turns are accounted completely separately.
MAX_LLM_TURNS_PER_HUMAN_TURN = 4

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

If the draft already has KPIs (e.g. merged in from the multi-agent research fan-out that \
ran before your first turn this session — each already has a name, weight, and full \
11-level guidelines, grounded in real research), your job THIS TURN is RECONCILIATION, \
not fresh generation: review the set for genuine quality/coverage gaps or true near-\
duplicates, do a final sanity pass on the weights (sibling groups must sum to 100 — see \
"Fields still missing/incomplete" below), and present it to the user for confirmation/\
adjustment via ask_clarification rather than inventing an entirely new KPI list from \
scratch. Only propose ADDITIONAL new KPIs via update_draft if there is a real, identified \
coverage gap the research didn't touch — do not discard or rewrite an already-researched \
KPI's guidelines just to "improve" them unless the user specifically asked you to change \
that KPI. If the ONLY thing missing is a scalar field (name/purpose/domain/audience/\
target_score, NOT the KPIs themselves), send update_draft with JUST those field(s) in \
`patch` and OMIT `kpis` entirely — do not re-type the existing KPI list just to fill in \
an unrelated field; omitting `kpis` from `patch` leaves every already-merged KPI exactly \
as it is, automatically. Re-typing a large KPI list from memory risks silently truncating \
it, which would undo the whole point of the research merge.

If the draft has NO KPIs yet (e.g. the research fan-out found nothing for this domain, or \
was skipped), prefer `update_draft` with your own best-guess proposal before you \
`ask_clarification` (LLM first, human second — propose candidate KPIs from what the user \
already told you, then ask about what's genuinely ambiguous or missing).

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
{research_context}"""


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
    `decide_research_angles` call (see `_decide_research_angles`) — this is deliberately
    NOT the structured `draft`, since on a session's first turn the draft is still entirely
    empty and the user's own words are the only real signal for the domain. Internal "tool"
    role notes (validation-error bookkeeping) are skipped as noise for this purpose."""
    relevant = [t for t in messages if t["role"] in ("user", "assistant")][-max_turns:]
    return "\n".join(f"{turn['role'].capitalize()}: {turn['content']}" for turn in relevant)


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
        results = await find_similar_scorecards(db, bedrock, query_text, top_n=3)
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
        if turn["role"] == "tool":
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


async def propose_kpis(state: BuilderState, config: RunnableConfig) -> dict[str, Any]:
    draft = ScorecardDraft.model_validate(state["draft"])
    turn_count = state.get("llm_turn_count", 0)
    session_id: str = state["session_id"]
    turn_started_at: datetime | None = (config.get("configurable") or {}).get("turn_started_at")

    if turn_count >= MAX_LLM_TURNS_PER_HUMAN_TURN:
        # Safety cutoff (see MAX_LLM_TURNS_PER_HUMAN_TURN): force a pause instead of
        # calling the model again, so a model that never calls ask_clarification/confirm
        # can't loop this node forever within one human turn.
        pending_tool = {
            "name": "ask_clarification",
            "input": {
                "question": (
                    "I've made several updates — let's pause here. What would you like "
                    "to adjust or add next?"
                ),
                "options": [],
                "missing_fields": draft.missing_fields(),
            },
        }
        # The persisted/returned chat message must be the actual question text the user
        # sees (this is what the frontend renders as the assistant's bubble content — see
        # _to_turn_result/ChatTurnRead.assistant_message) — never an internal debug label;
        # the cutoff itself is still logged server-side below for diagnosis.
        assistant_note = pending_tool["input"]["question"]
        logger.warning("propose_kpis hit MAX_LLM_TURNS_PER_HUMAN_TURN; forcing a pause.")
        return {
            "pending_tool": pending_tool,
            "messages": [{"role": "assistant", "content": assistant_note}],
            "llm_turn_count": turn_count + 1,
        }

    configurable = config.get("configurable", {})
    bedrock: BedrockClientProtocol = configurable["bedrock_client"]
    model_id: str | None = configurable.get("chat_model_id")
    web_search_client: WebSearchClientProtocol | None = configurable.get("web_search_client")
    jev_client: JevClientProtocol | None = configurable.get("jev_client")

    # Local working copy of the turn's conversation, extended in-place across any
    # web_search iterations below (the model's own tool call + the results fed back to
    # it) so it keeps full context within this node visit. Only the NEW entries appended
    # here (plus the final assistant_note) are returned at the end — state["messages"]
    # uses an append-only reducer (see _append), so returning the whole list would
    # duplicate everything already checkpointed.
    local_messages: list[ChatTurn] = list(state["messages"])
    search_calls_made = 0
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

    for gate_attempt in range(MAX_QUALITY_GATE_RETRIES + 1):
        result = None
        while True:
            session_budget_available = (
                web_search_calls_used_before + search_calls_made < MAX_WEB_SEARCH_CALLS_PER_SESSION
            )
            search_budget_available = (
                web_search_client is not None
                and search_calls_made < MAX_WEB_SEARCH_CALLS_PER_PROPOSE
                and session_budget_available
            )
            tools = _TOOLS_WITH_SEARCH if search_budget_available else _TOOLS

            scoring_formula_context = (
                f'\nCurrent custom scoring_formula: {draft.scoring_formula!r} '
                "(non-null means the default weighted average is currently OVERRIDDEN).\n"
                if draft.scoring_formula
                else "\nCurrent custom scoring_formula: none set (using the default weighted average).\n"
            )
            system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
                draft_json=json.dumps(draft.model_dump(mode="json")),
                missing_fields=draft.missing_fields() or "(none — draft looks complete)",
                research_context=f"\n{research_context}\n" if research_context else "",
                scoring_formula_context=scoring_formula_context,
            )
            if web_search_client is not None and not search_budget_available:
                # Forced-closure step (see MAX_WEB_SEARCH_CALLS_PER_PROPOSE /
                # MAX_WEB_SEARCH_CALLS_PER_SESSION): web_search is no longer offered in `tools`
                # at all, so the model literally cannot call it again — force_tool_use means it
                # must pick one of the remaining tools. This instruction just makes the "why"
                # explicit to the model too.
                budget_reason = (
                    "you've used this session's whole web research budget"
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
            tool_input = result.tool_input or {}
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
            gate_instruction = _conversation_context_text(local_messages) or "(nothing yet)"
            gate_answer = _final_answer_gate_text(result.tool_name, tool_input, assistant_note)
            gate = await quality_gate(jev_client, instruction=gate_instruction, answer=gate_answer)

            if gate.degraded:
                await emit_turn_event(
                    session_id, turn_started_at, "master", "quality_gate",
                    "Response quality check: Jev was unreachable — gate treated as passed "
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
                local_messages.append(
                    {
                        "role": "tool",
                        "content": (
                            f"[quality gate] Your last {result.tool_name} response scored "
                            f"{gate.score:.2f}/1.0 (threshold {QUALITY_GATE_THRESHOLD}) on an "
                            "automated check of how well it serves the user's actual request. "
                            "Reconsider and provide a genuinely improved response now."
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
    new_messages.append({"role": "assistant", "content": assistant_note})

    if pending_tool.get("name") == "ask_clarification":
        completed_message = f"Asking: {pending_tool.get('input', {}).get('question', '')}"
    elif pending_tool.get("name") == "respond_conversationally":
        completed_message = "Responding conversationally — draft left unchanged."
    elif (pending_tool.get("input") or {}).get("confirmed"):
        completed_message = "Draft confirmed — saving the scorecard."
    else:
        completed_message = "Updated the draft."
    await emit_turn_event(session_id, turn_started_at, "master", "completed", completed_message)

    return {
        "pending_tool": pending_tool,
        "messages": new_messages,
        "llm_turn_count": turn_count + 1,
        "web_search_calls_used": web_search_calls_used_before + search_calls_made,
    }


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
    if isinstance(patch.get("kpis"), list):
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

    current = ScorecardDraft.model_validate(state["draft"])
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

    return {
        "draft": new_draft.model_dump(mode="json"),
        "pending_tool": None,
        "last_patch_error": None,
        "status": new_status,
        "messages": [{"role": "tool", "content": note}],
    }


def _route_after_update(state: BuilderState) -> str:
    return "confirm" if state.get("status") == "ready_to_confirm" else "propose_kpis"


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
    graph.add_edge("research_kpis", "propose_kpis")
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
    graph.add_edge("ask_clarification", "propose_kpis")
    # respond_conversationally mirrors ask_clarification's own edge exactly (see its
    # docstring): it pauses via interrupt(), then resumes straight back into propose_kpis
    # once the user's next real message arrives — never routes anywhere else.
    graph.add_edge("respond_conversationally", "propose_kpis")
    graph.add_conditional_edges(
        "update_draft",
        _route_after_update,
        {"confirm": "confirm", "propose_kpis": "propose_kpis"},
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

    async def _ensure_ready(self) -> None:
        if self._compiled is not None:
            return
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
    assistant_note = None
    for turn in reversed(messages):
        if turn.get("role") in ("assistant", "tool"):
            assistant_note = turn.get("content")
            break

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
        return None
    state = dict(snapshot.values)
    if snapshot.interrupts:
        state["__interrupt__"] = list(snapshot.interrupts)
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
    await get_graph_manager().adelete_thread(session_id)
