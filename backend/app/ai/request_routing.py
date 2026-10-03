"""Cheap request routing for the scorecard builder: decides, WITHOUT the main chat model,
whether the user's message could contain their own KPI list (so the expensive
`classify_request` KPI-tree extraction on GLM-5 is worth running at all), and extracts a
requested KPI count.

Design (a cascade — cheapest, most-certain step first; anything uncertain escalates to the
main model, so this module can only ever REMOVE work, never change an answer):

1. **Deterministic heuristic gate** (`plausibly_contains_kpi_list`, zero model calls). Text
   with no list-like structure and no KPI vocabulary is `open_ended` outright. This is the
   common case ("make me a scorecard for incident response quality").
2. **Option-picker** (`route_request`) for text that passes the gate: pick ONE of the fixed
   labels `user_specified | hybrid | open_ended | unsure`. Tried in order:
   - **Jev** `choice` question (TypeSafe's decision model via OpenRouter — see
     `jev_client.py`). Jev natively supports "pick 1 of N labeled options" and returns a
     probability per option plus a confidence, in roughly 70-500 ms, which makes it a
     better fit for this job than for the instruction/answer scoring it was first used
     for. Only a CONFIDENT answer is used.
   - the small Bedrock judge model (`settings.bedrock_judge_model_id`, GLM-4.7-Flash) with
     a forced single-tool enum answer, when Jev is unconfigured, fails or is unsure.
3. Only when the router answers `open_ended` with confidence does the caller skip the main
   model; `user_specified`, `hybrid`, `unsure`, any failure, and any strong structural
   list evidence (2+ bullet lines) all fall through to the full extraction. The harmful
   error is a real KPI list misread as open_ended (the user's KPIs would be redesigned away),
   which is why only a confident `open_ended` is ever trusted and the extraction stays the
   source of truth for the final mode.

Unverified against live services: Jev's `choice` calibration on this task and the small
model's tool-call adherence were not measured here (no live calls) — hence the conservative
"only a confident open_ended skips extraction" rule and the `settings.request_router` kill
switch (`off` = always run the extraction).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.ai.bedrock_client import BedrockClientProtocol, ToolSpec
from app.config import get_settings

logger = logging.getLogger(__name__)

ROUTE_LABELS = ("user_specified", "hybrid", "open_ended", "unsure")
# Jev's own 0-1 confidence ((n * top_p - 1) / (n - 1)) must reach this for its pick to count.
JEV_MIN_CONFIDENCE = 0.6

_KPI_LIST_KEYWORDS = re.compile(r"\b(kpis?|metrics?|criteria|indicators?|measures?)\b", re.IGNORECASE)
_BULLET_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)


def _colon_items(text: str) -> int:
    after_colon = re.search(r":\s*(.+)$", text, re.DOTALL)
    if not after_colon:
        return 0
    items = [i for i in re.split(r"[,;\n]| and ", after_colon.group(1)) if 0 < len(i.split()) <= 6]
    return len(items)


def plausibly_contains_kpi_list(text: str, *, first_turn: bool = False) -> bool:
    """Cheap, deterministic gate: could `text` contain a user-supplied KPI list? Deliberately
    permissive — a false positive costs one cheap router call; a false negative only means the
    user's list is handled by the prompt-level guidance (and the model's `user_specified_kpis`
    declaration) instead of the extraction. True for: 2+ bullet/numbered lines; a KPI-ish
    keyword plus 2+ list separators; or a colon followed by 3+ short items. `first_turn=True`
    (the session's opening message, where a short "KPIs: A, B" list is typical) also accepts a
    KPI keyword with a colon list of 2+ items."""
    if len(_BULLET_LINE.findall(text)) >= 2:
        return True
    separators = text.count(",") + text.count(";") + text.count("\n") + len(re.findall(r"\band\b", text, re.I))
    if len(text) > 40 and _KPI_LIST_KEYWORDS.search(text) and separators >= 2:
        return True
    # A plain newline-separated list with no bullets, keyword or colon ("Use case...\nMTTR\nFirst
    # response time\n...") — 4+ short lines.
    short_lines = [ln for ln in text.splitlines() if ln.strip() and len(ln.split()) <= 10]
    if len(short_lines) >= 4:
        return True
    items = _colon_items(text)
    if items >= 3:
        return True
    return first_turn and items >= 2 and bool(_KPI_LIST_KEYWORDS.search(text))


def has_strong_list_structure(text: str) -> bool:
    """2+ bullet/numbered lines — evidence strong enough that no cheap router may veto the
    full extraction."""
    return len(_BULLET_LINE.findall(text)) >= 2


# --- requested KPI count ----------------------------------------------------------------

_NUMBER_WORDS = {
    "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty-five": 25, "twenty five": 25,
    "thirty": 30, "thirty-five": 35, "thirty five": 35, "forty": 40, "forty-five": 45,
    "forty five": 45, "fifty": 50, "fifty five": 55, "sixty": 60, "seventy": 70,
    "seventy-five": 75, "seventy five": 75, "eighty": 80, "ninety": 90, "hundred": 100,
    "one hundred": 100,
}
_NOUN = r"(?:kpis?|metrics?|indicators?|criteria|measures?)"
_NUM = r"(\d{1,3}|" + "|".join(sorted((re.escape(w) for w in _NUMBER_WORDS), key=len, reverse=True)) + r")"
# "35 KPIs", "around 35 good KPIs", "at least forty metrics" — the KPI noun must follow the
# number within 3 words, so "within 30 days" or "Q3 plan" never match.
_COUNT = re.compile(rf"\b{_NUM}\s+(?:[\w'-]+\s+){{0,3}}?{_NOUN}\b", re.IGNORECASE)
_RANGE = re.compile(
    rf"\b(\d{{1,3}})\s*(?:-|–|to|and)\s*(\d{{1,3}})\s+(?:[\w'-]+\s+){{0,3}}?{_NOUN}\b", re.IGNORECASE
)
MIN_TARGET, MAX_TARGET = 2, 500
# "5 KPIs per category", "3 KPIs for each team" (a per-group quota, not a total) and "top 3 KPIs" /
# "first 5 metrics" (a selection from the answer, not the scorecard size) are NOT a requested count.
_PER_GROUP_AFTER = re.compile(
    r"^\s*(?:per|for\s+each|in\s+each|under\s+each|for\s+every|each|every|apiece)\b", re.IGNORECASE
)
_SELECTION_BEFORE = re.compile(r"\b(?:top|first|best|main|biggest)\s+$", re.IGNORECASE)


def _is_per_group_or_selection(text: str, match: re.Match[str]) -> bool:
    return bool(_PER_GROUP_AFTER.match(text[match.end():])) or bool(
        _SELECTION_BEFORE.search(text[: match.start()])
    )


def _to_int(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def extract_kpi_target(text: str) -> int | None:
    """The KPI count the user asked for ("give me 35 KPIs", "40-50 metrics" -> 45), or None.
    Deterministic (no model call). The LAST such statement in `text` wins (a later message
    revising "make it 50" overrides an earlier "30")."""
    found: int | None = None
    best_pos = -1
    range_spans: list[tuple[int, int]] = []
    for match in _RANGE.finditer(text):
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo < hi and not _is_per_group_or_selection(text, match):
            range_spans.append(match.span())
            if match.start() >= best_pos:
                found, best_pos = (lo + hi + 1) // 2, match.start()
    for match in _COUNT.finditer(text):
        if any(a <= match.start() < b for a, b in range_spans):
            continue  # the number is part of a "30-40 KPIs" range already handled above
        if _is_per_group_or_selection(text, match):
            continue
        value = _to_int(match.group(1))
        if value is not None and match.start() >= best_pos:
            found, best_pos = value, match.start()
    if found is None or not (MIN_TARGET <= found <= MAX_TARGET):
        return None
    return found


# --- option-picker -------------------------------------------------------------------------

ROUTE_REQUEST_TOOL = ToolSpec(
    name="route_request",
    description=(
        "Pick exactly ONE label for the user's message. user_specified: they listed their own "
        "KPIs/metrics by name. hybrid: they listed some KPIs AND asked for more to be suggested. "
        "open_ended: they only described a goal/domain; NO KPI names are given. unsure: you "
        "cannot tell. Choose open_ended ONLY if you are certain no KPI names are listed."
    ),
    input_schema={
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": list(ROUTE_LABELS)}},
        "required": ["mode"],
    },
)

_ROUTE_SYSTEM_PROMPT = """Classify the user's message into ONE label. Do not extract anything.
- user_specified: the user wrote out their own KPIs/metrics/criteria (inline, bullets or a table).
- hybrid: the user wrote out some of their own KPIs AND asked for more to be suggested.
- open_ended: the user only described a goal, domain or problem; no KPI names are given \
(topics or examples mentioned in passing are NOT a list).
- unsure: you cannot tell.
Pick open_ended only when you are certain. Call `route_request` exactly once."""

_JEV_OPTIONS = {
    "user_specified": "The user wrote out their own list of KPIs/metrics/criteria by name.",
    "hybrid": "The user wrote out some of their own KPIs and also asked for more KPIs to be suggested.",
    "open_ended": "The user only described a goal, domain or problem; no KPI names are listed.",
}


@dataclass(frozen=True)
class RouteDecision:
    """`mode` is one of ROUTE_LABELS; `source` records which step decided (gate | jev |
    small_model | fallthrough | forced) for logging and tests."""

    mode: str
    source: str

    @property
    def skip_extraction(self) -> bool:
        return self.mode == "open_ended"


async def route_request(
    text: str,
    *,
    bedrock: BedrockClientProtocol | None,
    jev_client: Any | None,
    first_turn: bool = True,
) -> RouteDecision:
    """The cascade described in the module docstring. NEVER raises: any failure yields
    `unsure` (= run the full extraction)."""
    import asyncio

    router = get_settings().request_router.lower()
    if router == "off":
        return RouteDecision("unsure", "forced")
    if not plausibly_contains_kpi_list(text, first_turn=first_turn):
        return RouteDecision("open_ended", "gate")
    if has_strong_list_structure(text):
        return RouteDecision("unsure", "forced")

    if router in ("auto", "jev") and jev_client is not None and hasattr(jev_client, "choose"):
        try:
            picked = await jev_client.choose(
                state={"user_message": text},
                instructions="Which kind of request is this user message?",
                options=_JEV_OPTIONS,
            )
            if picked.confidence >= JEV_MIN_CONFIDENCE:
                return RouteDecision(picked.choice, "jev")
        except Exception:  # noqa: BLE001 — Jev unreachable/unconfigured: fall to the small model
            logger.info("route_request: Jev choice unavailable; trying the small model.", exc_info=True)

    if router in ("auto", "small_model") and bedrock is not None:
        try:
            result = await asyncio.to_thread(
                bedrock.converse,
                messages=[{"role": "user", "content": [{"text": text[:4000]}]}],
                system=_ROUTE_SYSTEM_PROMPT,
                tools=[ROUTE_REQUEST_TOOL],
                force_tool_use=True,
                model_id=get_settings().bedrock_judge_model_id,
            )
            if result.is_tool_use and result.tool_name == "route_request" and not result.truncated:
                mode = str((result.tool_input or {}).get("mode") or "")
                if mode in ROUTE_LABELS:
                    return RouteDecision(mode, "small_model")
        except Exception:  # noqa: BLE001 — any failure => run the full extraction
            logger.info("route_request: small-model routing failed; falling through.", exc_info=True)
    return RouteDecision("unsure", "fallthrough")


# --- deterministic fast path for clearly structured KPI lists -----------------------------------
#
# The main-model extraction (`classify_request` on GLM-5) re-types every KPI name as tool output;
# for a 35-KPI bullet list that took 1-2 MINUTES. When the list is unmistakably structured and
# every item is a plain name, a regex parse gives the identical result (names exact, hierarchy
# from indentation) in microseconds. Anything with weights, definitions, parentheses, prose
# between items, or a "suggest more" cue is NOT parsed here (returns None) and escalates to the
# model extraction, so this can only remove work, never change an answer.

_BULLET_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?:[-*•]|\d+[.)])[ \t]+(?P<text>\S.*?)\s*$")
_PLAIN_NAME = re.compile(r"^[A-Za-z][\w &/'’.+\-]*$")
_NAME_REJECT = re.compile(r"\d+\s*%|\bweights?\b|\bthreshold\b| [-–—] |[–—]", re.IGNORECASE)
_WANTS_MORE = re.compile(
    r"\b(suggest|recommend|propose|come up with|add (?:some|more|any|others?)|additional|others?|more|extra|"
    r"missing|complement\w*|fill (?:in )?the gaps?)\b[^.\n]{0,40}\b(kpis?|metrics?|criteria|indicators?)\b",
    re.IGNORECASE,
)
_INLINE_CUE = re.compile(
    r"\b(?:kpis?|metrics?|criteria|indicators?)\b[^:\n]{0,40}:[ \t]*(?P<rest>[^\n]+)", re.IGNORECASE
)
MAX_NAME_WORDS = 8


def _plain_name(text: str, *, max_words: int = MAX_NAME_WORDS) -> bool:
    return (
        0 < len(text.split()) <= max_words
        and bool(_PLAIN_NAME.match(text))
        and not _NAME_REJECT.search(text)
    )


def _width(indent: str) -> int:
    return len(indent.replace("\t", "    "))


def _parse_bullets(text: str) -> list[dict[str, Any]] | None:
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if _BULLET_ITEM.match(ln)), None)
    if first is None:
        return None
    last = max(i for i, ln in enumerate(lines) if _BULLET_ITEM.match(ln))
    entries: list[tuple[int, str]] = []
    for ln in lines[first : last + 1]:
        if not ln.strip():
            continue
        m = _BULLET_ITEM.match(ln)
        if m is None:
            return None  # prose / a category header between items: let the model read it
        entries.append((_width(m.group("indent")), m.group("text")))
    if len(entries) < 3:
        return None
    items: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []  # (indent, name) of the open ancestors
    for i, (indent, name) in enumerate(entries):
        has_children = i + 1 < len(entries) and entries[i + 1][0] > indent
        if has_children and name.endswith(":"):
            name = name[:-1].rstrip()
        if not _plain_name(name):
            return None
        while stack and stack[-1][0] >= indent:
            stack.pop()
        items.append({"name": name, "parent_name": stack[-1][1] if stack else None})
        stack.append((indent, name))
    return items


def _parse_inline(text: str) -> list[dict[str, Any]] | None:
    m = _INLINE_CUE.search(text)
    if m is None:
        return None
    rest = re.split(r"\.(?:\s|$)|\?", m.group("rest"), maxsplit=1)[0]
    parts = [p.strip() for p in re.split(r",|;|\band\b", rest) if p.strip()]
    if len(parts) < 3 or any(not _plain_name(p, max_words=5) or re.search(r"\d", p) for p in parts):
        return None
    return [{"name": p, "parent_name": None} for p in parts]


def parse_structured_kpi_list(text: str) -> list[dict[str, Any]] | None:
    """Deterministically extract a clearly structured KPI list (bullets / numbered lines with
    optional indentation hierarchy, or a plain comma list right after a "KPIs:" cue) as
    `[{name, parent_name}]` in order — or None when ANYTHING is unclear (weights, definitions,
    parentheses, prose between items, a "suggest more" request, or a requested KPI count
    larger than the list), in which case the caller must use the model extraction."""
    if not text or _WANTS_MORE.search(text):
        return None
    items = _parse_bullets(text) or _parse_inline(text)
    if not items:
        return None
    parents = {i["parent_name"] for i in items if i["parent_name"]}
    leaves = sum(1 for i in items if i["name"] not in parents)
    wanted = extract_kpi_target(text)
    if wanted is not None and wanted > leaves:
        return None  # "I need 50 KPIs" with 35 listed: they want more — a hybrid request
    return items
