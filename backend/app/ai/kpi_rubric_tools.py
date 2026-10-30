"""Rubric tools of the chat assistant (app/ai/chat_agent.py): review, research and rewrite the 0-10 guidelines of ONE
KPI, a hand-picked set, or a whole category, without re-running the scorecard builder.

Why: evaluations are scored by Jev (app/pipeline/jev_scorer.py), which places the evidence on the KPI's guideline
levels. Where neighbouring levels are not separable by what a submission can show, Jev spreads its answer over
several levels and its confidence drops (seen live: RADIX scorecard levels such as "Both declarations stated
clearly" / "... signed or attributed" averaged 0.2-0.3 confidence). The fix is in the rubric, so the assistant gets
small tools it can chain in one message:

- `review_rubrics`: per KPI, the MEASURED Jev confidence from this chart's past evaluations (the reliable signal) plus
  text checks (missing / identical / near-duplicate levels, terse levels, no countable condition, thresholds that just
  restate the level number, too few distinct thresholds). Worst first.
- `research_kpis`: one focused web search per KPI; the results feed `rewrite_guidelines`.
- `rewrite_guidelines`: the builder's compact-rubric writer (`scorecard_builder._fill_missing_guidelines`, on the
  builder's model) rewrites the chosen KPIs, given their current rubric, its problems, the measured confidence, the
  assistant's instructions and the research. A KPI the writer fails on keeps its old rubric (never the generic
  fallback).
- `set_kpi_guidelines`: the assistant writes exact level texts / thresholds itself, for any subset of levels.

Every tool here is a lookup in the assistant's loop: edits apply to the draft immediately (like the scoring formula)
and come back as a tool result, so the assistant can fix one category, check, then fix the next. Weights and the KPI
tree are never touched here (that stays with `edit_kpis`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Float, case, func, select

from app.ai import scorecard_builder as sb
from app.ai.bedrock_client import BedrockClientProtocol, ToolSpec
from app.ai.draft_schema import MAX_SCORE_LEVEL, MIN_SCORE_LEVEL, GuidelineDraft, KpiDraft, ScorecardDraft
from app.ai.web_search import SearchResult, WebSearchClientProtocol
from app.db import AsyncSessionLocal
from app.models.chat_session import ChatSession
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_node import KpiNode
from app.models.scorecard_version import ScorecardVersion

logger = logging.getLogger(__name__)

MAX_REWRITE_KPIS = 30  # KPIs per rewrite_guidelines call (a whole category normally fits)
MAX_SET_KPIS = 20  # KPIs per set_kpi_guidelines call
MAX_RESEARCH_KPIS = 8  # web searches per research_kpis call
MAX_REVIEW_ROWS = 40  # KPIs listed by one review_rubrics call
# Jev confidence below this means the scorer could not settle on one level (see app/pipeline/jev_scorer.py).
LOW_CONFIDENCE = 0.35
# Median words per level below which a rubric counts as terse.
MIN_LEVEL_WORDS = 6
NEAR_DUPLICATE = 0.8  # word-set overlap of two adjacent levels above which they read as the same level

_SCORE_LEVELS = [str(i) for i in range(MIN_SCORE_LEVEL, MAX_SCORE_LEVEL + 1)]
_FALLBACK_MARKER = "Auto-generated fallback rubric"
_CIRCULAR_METRIC = re.compile(r"\b(score|rating|rubric|points?)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9%]+")

# --- tool specs ---------------------------------------------------------------------------------------------------

_TARGET_PROPERTIES: dict[str, Any] = {
    "names": {
        "type": "array", "items": {"type": "string"}, "maxItems": MAX_REWRITE_KPIS,
        "description": "Exact KPI names from the draft.",
    },
    "category": {
        "type": "string",
        "description": "A category name from the draft: every scored KPI under it (any depth) is included.",
    },
}

REVIEW_RUBRICS_TOOL = ToolSpec(
    name="review_rubrics",
    description=(
        "Check how well KPI guidelines work for scoring, worst first. Per KPI: how sure the evaluation scorer (Jev) "
        "was on this chart's past evaluations (avg confidence 0-1; below 0.35 means its levels could not be told "
        "apart for the evidence submissions give), plus problems found in the level texts. Give `names`, a "
        "`category`, or neither for every KPI. Call it before rewriting, and again after to check the result."
    ),
    input_schema={"type": "object", "properties": _TARGET_PROPERTIES},
)

RESEARCH_KPIS_TOOL = ToolSpec(
    name="research_kpis",
    description=(
        "One focused web search per KPI (up to 8) for how that KPI is measured in practice: benchmarks, standard "
        "thresholds, observable behaviours. The results are kept for this message and used by rewrite_guidelines "
        "for those KPIs. Use specific queries (domain + metric + 'benchmark' / 'rubric' / 'threshold')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kpis": {
                "type": "array", "minItems": 1, "maxItems": MAX_RESEARCH_KPIS,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact KPI name from the draft."},
                        "query": {"type": "string", "description": "The web search query for this KPI."},
                    },
                    "required": ["name", "query"],
                },
            },
        },
        "required": ["kpis"],
    },
)

REWRITE_GUIDELINES_TOOL = ToolSpec(
    name="rewrite_guidelines",
    description=(
        "Rewrite the full 0-10 guidelines of one KPI, several, or a whole category (`names` and/or `category`, up to "
        "30 KPIs). The rubric writer gets each KPI's current rubric, its review problems, the scorer's measured "
        "confidence, your `instructions` and any research_kpis results for it, and writes 11 distinct levels with "
        "numeric thresholds. Applied to the draft at once; the result lists what changed. Weights are untouched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **_TARGET_PROPERTIES,
            "instructions": {
                "type": "string",
                "description": (
                    "What the new rubrics must fix or keep, e.g. 'make each level a count of documented items the "
                    "submission can show; keep level 0 = nothing submitted'. Applies to every KPI in this call."
                ),
            },
        },
        "required": ["instructions"],
    },
)

SET_KPI_GUIDELINES_TOOL = ToolSpec(
    name="set_kpi_guidelines",
    description=(
        "Write exact guideline levels yourself, for one KPI or several (up to 20), changing only the levels you "
        "give (e.g. just 4-7 of one KPI). Each level: `text` (the observable condition for that score) and "
        "optionally `criteria` (a numeric threshold; null removes it; omitted keeps the current one). Applied to the "
        "draft at once; rejected as a whole if any level would duplicate another level of the same KPI."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "edits": {
                "type": "array", "minItems": 1, "maxItems": MAX_SET_KPIS,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact KPI name from the draft."},
                        "levels": {
                            "type": "object",
                            "description": 'Keyed by score level "0".."10".',
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "criteria": {
                                        "type": ["object", "null"],
                                        "properties": {
                                            "metric": {"type": "string"},
                                            "operator": {"type": "string", "enum": [">=", "<=", "=="]},
                                            "value": {"type": "number"},
                                            "unit": {"type": "string"},
                                        },
                                        "required": ["metric", "value"],
                                    },
                                },
                                "required": ["text"],
                            },
                        },
                    },
                    "required": ["name", "levels"],
                },
            },
        },
        "required": ["edits"],
    },
)

TOOLS = [REVIEW_RUBRICS_TOOL, RESEARCH_KPIS_TOOL, REWRITE_GUIDELINES_TOOL, SET_KPI_GUIDELINES_TOOL]
TOOL_NAMES = frozenset(t.name for t in TOOLS)

# Shared by every rewrite: the levels Jev places evidence on must be separable by what a submission shows.
_REWRITE_RULE = (
    "Rewrite this KPI's rubric so an evaluator reading only quoted evidence from a submission can tell every level "
    "from its neighbours: each level states an observable condition with the count, percentage or time that "
    "separates it (about 10-20 words), never a 'reviewer score' that just restates the level number. Level 0 = "
    "nothing relevant shown. Keep what the KPI measures."
)


# --- selection ----------------------------------------------------------------------------------------------------


def _key(name: str) -> str:
    return name.strip().casefold()


def _parents(draft: ScorecardDraft) -> set[str]:
    return {k.parent_name for k in draft.kpis if k.parent_name}


def leaves(draft: ScorecardDraft) -> list[KpiDraft]:
    """Every leaf KPI (the ones that carry guidelines), in draft order."""
    parents = _parents(draft)
    return [k for k in draft.kpis if k.name not in parents]


def select_targets(draft: ScorecardDraft, names: Any, category: Any, limit: int) -> tuple[list[KpiDraft], list[str]]:
    """The leaf KPIs `names` and/or `category` point at (all leaves when neither is given), de-duplicated in draft
    order, capped at `limit`. Returns `(kpis, problems)`; an unknown name / category, a category named as a KPI or
    going over the limit is reported, never guessed."""
    all_leaves = leaves(draft)
    parents = _parents(draft)
    by_key = {_key(k.name): k for k in draft.kpis}
    wanted: set[str] = set()
    problems: list[str] = []
    given_names = [str(n) for n in names] if isinstance(names, list) else []
    for name in given_names:
        kpi = by_key.get(_key(name))
        if kpi is None:
            problems.append(f'unknown KPI "{name}"')
        elif kpi.name in parents:
            problems.append(f'"{kpi.name}" is a category: pass it as `category`')
        else:
            wanted.add(kpi.name)
    cat = str(category).strip() if isinstance(category, str) else ""
    if cat:
        node = by_key.get(_key(cat))
        if node is None or node.name not in parents:
            problems.append(f'unknown category "{cat}"')
        else:
            under = sb._descendants([k.model_dump(mode="json") for k in draft.kpis], node.name)
            wanted |= {k.name for k in all_leaves if k.name in under}
    if not given_names and not cat:
        wanted = {k.name for k in all_leaves}
    chosen = [k for k in all_leaves if k.name in wanted]
    if len(chosen) > limit:
        problems.append(f"{len(chosen)} KPIs selected; only the first {limit} were used (call again for the rest)")
        chosen = chosen[:limit]
    return chosen, problems


# --- text checks --------------------------------------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _threshold(g: GuidelineDraft) -> float | None:
    value = (g.quantitative_criteria or {}).get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def lint_rubric(kpi: KpiDraft) -> list[str]:
    """Problems in a KPI's level texts that make neighbouring levels hard to separate. Empty = none found (which
    does not prove the levels separate well: the measured Jev confidence is the real test)."""
    g = kpi.guidelines
    if not g:
        return ["no guidelines"]
    issues: list[str] = []
    missing = [lv for lv in _SCORE_LEVELS if lv not in g]
    if missing:
        issues.append(f"missing levels {', '.join(missing)}")
    present = [lv for lv in _SCORE_LEVELS if lv in g]
    texts = {lv: g[lv].qualitative_text.strip() for lv in present}
    if any(_FALLBACK_MARKER in t for t in texts.values()):
        return [*issues, "generic fallback rubric (written when the rubric writer failed): rewrite it"]
    seen: dict[str, str] = {}
    for lv in present:
        norm = " ".join(sorted(_words(texts[lv])))
        if norm in seen:
            issues.append(f"levels {seen[norm]} and {lv} say the same thing")
        else:
            seen[norm] = lv
    for a, b in zip(present, present[1:], strict=False):
        wa, wb = _words(texts[a]), _words(texts[b])
        # Levels that differ by a number ("3 of 5 documented" / "4 of 5 documented") are what a rubric should be.
        by_number = any(any(ch.isdigit() for ch in w) for w in wa ^ wb)
        if wa and wb and wa != wb and not by_number and len(wa & wb) / len(wa | wb) >= NEAR_DUPLICATE:
            issues.append(f"levels {a} and {b} differ by only a word or two")
    scored = [lv for lv in present if lv != "0"]
    counts = sorted(len(texts[lv].split()) for lv in scored)
    if counts and counts[len(counts) // 2] < MIN_LEVEL_WORDS:
        issues.append(f"levels are terse (median {counts[len(counts) // 2]} words): state the observable condition")
    unmeasurable = [lv for lv in scored if not re.search(r"\d", texts[lv]) and _threshold(g[lv]) is None]
    if len(unmeasurable) >= 5:
        issues.append(f"{len(unmeasurable)} levels have no number or countable condition")
    def restates_level(lv: str) -> bool:
        crit = g[lv].quantitative_criteria or {}
        label = f"{crit.get('metric') or ''} {crit.get('unit') or ''}"
        return _threshold(g[lv]) == float(lv) and bool(_CIRCULAR_METRIC.search(label))

    circular = [lv for lv in scored if restates_level(lv)]
    if len(circular) >= 5:
        issues.append("thresholds just restate the level number (a 'reviewer score'), not something evidence shows")
    values = [_threshold(g[lv]) for lv in scored]
    numbers = [v for v in values if v is not None]
    if len(numbers) >= 6 and len(set(numbers)) <= 3:
        issues.append(
            f"only {len(set(numbers))} distinct thresholds over 10 levels (a pass/fail measure on a 0-10 scale)"
        )
    if len(numbers) >= 2 and numbers != sorted(numbers) and numbers != sorted(numbers, reverse=True):
        issues.append("thresholds are not in order from level 1 to 10")
    return issues


# --- measured scoring confidence ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class KpiHealth:
    """How Jev scored one KPI across this chart's evaluations (all versions, matched by KPI name)."""

    evaluations: int
    avg_confidence: float
    low_share: float  # share of scores with confidence below LOW_CONFIDENCE
    avg_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluations": self.evaluations,
            "avg_confidence": round(self.avg_confidence, 2),
            "low_confidence_share": round(self.low_share, 2),
            "avg_score": round(self.avg_score, 2),
        }


async def target_scorecard_id(session_id: str) -> uuid.UUID | None:
    """The saved chart this chat edits, if any."""
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        return None
    async with AsyncSessionLocal() as db:
        session = await db.get(ChatSession, sid)
        return session.target_scorecard_id if session is not None else None


async def scoring_health(scorecard_id: uuid.UUID | None) -> dict[str, KpiHealth]:
    """Per KPI name (casefolded): Jev's confidence on the chart's past evaluations. Empty when the chat edits no
    saved chart or it has no Jev-scored evaluations. Never raises (the review still runs on the text checks)."""
    if scorecard_id is None:
        return {}
    confidence = EvaluationKpiResult.jev_raw["confidence"].astext.cast(Float)
    stmt = (
        select(
            KpiNode.name,
            func.count(func.distinct(EvaluationKpiResult.evaluation_id)),
            func.avg(confidence),
            func.avg(case((confidence < LOW_CONFIDENCE, 1.0), else_=0.0)),
            func.avg(EvaluationKpiResult.score),
        )
        .join(KpiNode, KpiNode.id == EvaluationKpiResult.kpi_node_id)
        .join(Evaluation, Evaluation.id == EvaluationKpiResult.evaluation_id)
        .join(ScorecardVersion, ScorecardVersion.id == Evaluation.scorecard_version_id)
        .where(ScorecardVersion.scorecard_id == scorecard_id, EvaluationKpiResult.jev_raw.has_key("confidence"))
        .group_by(KpiNode.name)
    )
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(stmt)).all()
    except Exception:  # noqa: BLE001 - the review must still work without the numbers
        logger.warning("scoring_health: query failed for scorecard %s.", scorecard_id, exc_info=True)
        return {}
    return {
        _key(name): KpiHealth(int(n), float(conf or 0), float(low or 0), float(score or 0))
        for name, n, conf, low, score in rows
    }


# --- review -------------------------------------------------------------------------------------------------------


def review_text(draft: ScorecardDraft, args: dict[str, Any], health: dict[str, KpiHealth]) -> str:
    """`review_rubrics`' tool result: one JSON row per selected KPI, worst first (measured low confidence, then the
    number of text problems)."""
    kpis, problems = select_targets(draft, args.get("names"), args.get("category"), limit=len(draft.kpis) or 1)
    rows: list[dict[str, Any]] = []
    for kpi in kpis:
        h = health.get(_key(kpi.name))
        row: dict[str, Any] = {"name": kpi.name, "category": kpi.parent_name, "weight": kpi.weight}
        if h is not None:
            row["scorer"] = h.as_dict()
        row["problems"] = lint_rubric(kpi)
        rows.append(row)

    def badness(row: dict[str, Any]) -> tuple[float, int]:
        conf = (row.get("scorer") or {}).get("avg_confidence")
        return (conf if conf is not None else 1.0, -len(row["problems"]))

    rows.sort(key=badness)
    shown = rows[:MAX_REVIEW_ROWS]
    low = [r["name"] for r in rows if (r.get("scorer") or {}).get("avg_confidence", 1.0) < LOW_CONFIDENCE]
    head = [f"Reviewed {len(rows)} KPI(s)."]
    if health:
        head.append(
            "`scorer` = how the evaluation scorer (Jev) did on this chart's past evaluations: avg_confidence 0-1 "
            f"(below {LOW_CONFIDENCE} it could not settle on one level), low_confidence_share = share of scores below "
            f"that. Low confidence: {', '.join(low) if low else 'none'}."
        )
    else:
        head.append("No scored evaluations of this chart yet: only the text checks are available.")
    if len(rows) > len(shown):
        head.append(f"Showing the worst {len(shown)}; pass `names` or `category` to see others.")
    if problems:
        head.append("Note: " + "; ".join(problems) + ".")
    return "\n".join(head) + "\n" + json.dumps(shown, ensure_ascii=False)


# --- research -----------------------------------------------------------------------------------------------------


@dataclass
class RubricSession:
    """What the rubric tools keep across the steps of ONE assistant message: research results per KPI and the
    measured scorer health (loaded once)."""

    research: dict[str, list[SearchResult]] = field(default_factory=dict)
    health: dict[str, KpiHealth] | None = None


async def research(
    draft: ScorecardDraft,
    args: dict[str, Any],
    client: WebSearchClientProtocol,
    rs: RubricSession,
    budget: int,
) -> tuple[str, int]:
    """Runs `research_kpis`: one search per known KPI, concurrently, at most `budget` of them. Returns the tool-result
    text and the number of searches made. `search()` never raises by contract."""
    by_key = {_key(k.name): k for k in leaves(draft)}
    jobs: list[tuple[str, str]] = []
    notes: list[str] = []
    for item in args.get("kpis") or []:
        if not isinstance(item, dict):
            continue
        kpi = by_key.get(_key(str(item.get("name") or "")))
        query = str(item.get("query") or "").strip()
        if kpi is None:
            notes.append(f'unknown KPI "{item.get("name")}"')
        elif query:
            jobs.append((kpi.name, query))
    if len(jobs) > budget:
        notes.append(f"search budget left for this message: {budget}; skipped {len(jobs) - budget} KPI(s)")
        jobs = jobs[:budget]
    found = await asyncio.gather(*(client.search(q) for _, q in jobs))
    lines: list[str] = []
    for (name, query), results in zip(jobs, found, strict=True):
        rs.research.setdefault(name, []).extend(results)
        lines.append(f'## {name}\n{sb._format_search_results(query, results)}')
    if notes:
        lines.append("Note: " + "; ".join(notes) + ".")
    return ("\n\n".join(lines) or "No searches were run."), len(jobs)


def _findings(names: list[str], rs: RubricSession) -> list[sb.ResearchFinding] | None:
    out = [
        sb.ResearchFinding(
            category=f"KPI: {name}",
            summary=" ".join(r.snippet for r in rs.research[name])[:1500],
            sources=[{"title": r.title, "url": r.url} for r in rs.research[name][:5]],
        )
        for name in names
        if rs.research.get(name)
    ]
    return out or None


# --- rewrite / set ------------------------------------------------------------------------------------------------


def _rubric_line(kpi: KpiDraft) -> str:
    parts = []
    for lv in _SCORE_LEVELS:
        g = kpi.guidelines.get(lv)
        if g is None:
            continue
        crit = g.quantitative_criteria or {}
        num = f" [{crit.get('operator', '>=')} {crit.get('value')} {crit.get('unit') or ''}]".rstrip() if crit else ""
        parts.append(f"{lv}: {g.qualitative_text.strip()}{num}")
    return " | ".join(parts) or "(none)"


def _replace_guidelines(draft: ScorecardDraft, new: dict[str, dict[str, Any]]) -> ScorecardDraft:
    kpis = [
        k.model_copy(update={"guidelines": {lv: GuidelineDraft.model_validate(v) for lv, v in new[k.name].items()}})
        if k.name in new else k
        for k in draft.kpis
    ]
    return draft.model_copy(update={"kpis": kpis})


async def rewrite(
    draft: ScorecardDraft,
    args: dict[str, Any],
    rs: RubricSession,
    client: BedrockClientProtocol,
    model_id: str | None,
    context_text: str,
) -> tuple[ScorecardDraft, str]:
    """Runs `rewrite_guidelines`. Returns the (possibly) updated draft and the tool-result text."""
    kpis, problems = select_targets(draft, args.get("names"), args.get("category"), MAX_REWRITE_KPIS)
    if not kpis:
        return draft, "REJECTED: no KPI selected" + (f" ({'; '.join(problems)})" if problems else "") + "."
    instructions = str(args.get("instructions") or "").strip()
    health = rs.health or {}
    items = []
    for kpi in kpis:
        h = health.get(_key(kpi.name))
        guidance = [_REWRITE_RULE]
        if instructions:
            guidance.append(f"Instructions: {instructions}")
        guidance.append(f"Current rubric: {_rubric_line(kpi)}")
        issues = lint_rubric(kpi)
        if issues:
            guidance.append("Problems: " + "; ".join(issues))
        if h is not None:
            guidance.append(
                f"On {h.evaluations} past evaluations the scorer's avg confidence was {h.avg_confidence:.2f} "
                f"(0-1; low means neighbouring levels could not be told apart)."
            )
        items.append({"name": kpi.name, "parent_name": kpi.parent_name, "guidance": " ".join(guidance)})
    generated, failed = await sb._fill_missing_guidelines(
        items, client, model_id, context_text, _findings([k.name for k in kpis], rs)
    )
    applied = {name: g for name, g in generated.items() if name not in failed}
    new_draft = _replace_guidelines(draft, applied) if applied else draft
    by_name = {k.name: k for k in new_draft.kpis}
    lines = [f"Rewrote {len(applied)} of {len(kpis)} KPI rubric(s)."]
    if failed:
        lines.append(f"The rubric writer failed for {', '.join(failed)}: their old rubric is kept.")
    if problems:
        lines.append("Note: " + "; ".join(problems) + ".")
    for name in applied:
        after = lint_rubric(by_name[name])
        lines.append(f"- {name}: " + ("no text problems left" if not after else "still: " + "; ".join(after)))
        if len(applied) <= 5:
            lines.append(f"  {_rubric_line(by_name[name])}")
    if len(applied) > 5:
        lines.append("Use view_kpis to read the new levels.")
    return new_draft, "\n".join(lines)


def _criteria(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float) or not str(raw.get("metric") or "").strip():
        return None
    out: dict[str, Any] = {
        "metric": str(raw["metric"]).strip(),
        "operator": raw.get("operator") if raw.get("operator") in (">=", "<=", "==") else ">=",
        "value": int(value) if float(value).is_integer() else float(value),
    }
    if str(raw.get("unit") or "").strip():
        out["unit"] = str(raw["unit"]).strip()
    return out


def set_levels(draft: ScorecardDraft, args: dict[str, Any]) -> tuple[ScorecardDraft | None, str]:
    """Runs `set_kpi_guidelines`: merges the given levels into each KPI's rubric. All or nothing: any error rejects
    the whole call (returns `None` and the reasons)."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return None, "REJECTED: `edits` must list at least one KPI."
    leaf_by_key = {_key(k.name): k for k in leaves(draft)}
    categories = {_key(n) for n in _parents(draft)}
    errors: list[str] = []
    new: dict[str, dict[str, Any]] = {}
    changed: dict[str, list[str]] = {}
    for edit in edits[:MAX_SET_KPIS]:
        name = str((edit or {}).get("name") or "") if isinstance(edit, dict) else ""
        kpi = leaf_by_key.get(_key(name))
        if kpi is None:
            errors.append(f'"{name}" is a category (no guidelines)' if _key(name) in categories
                          else f'unknown KPI "{name}"')
            continue
        levels = edit.get("levels")
        if not isinstance(levels, dict) or not levels:
            errors.append(f'"{kpi.name}": no levels given')
            continue
        rubric = {lv: g.model_dump(mode="json") for lv, g in kpi.guidelines.items()}
        for lv_raw, spec in levels.items():
            lv = str(lv_raw).strip()
            text = str((spec or {}).get("text") or "").strip() if isinstance(spec, dict) else ""
            if lv not in _SCORE_LEVELS:
                errors.append(f'"{kpi.name}": level {lv_raw!r} is not 0-10')
                continue
            if not text:
                errors.append(f'"{kpi.name}" level {lv}: empty text')
                continue
            criteria = rubric.get(lv, {}).get("quantitative_criteria")
            if "criteria" in spec:
                criteria = _criteria(spec["criteria"])
                if spec["criteria"] is not None and criteria is None:
                    errors.append(f'"{kpi.name}" level {lv}: criteria need a metric and a numeric value')
                    continue
            rubric[lv] = {"qualitative_text": text, "quantitative_criteria": criteria}
            changed.setdefault(kpi.name, []).append(lv)
        missing = [lv for lv in _SCORE_LEVELS if lv not in rubric]
        if missing:
            errors.append(f'"{kpi.name}": levels {", ".join(missing)} have no text (give all 11 for a new rubric)')
        texts = [" ".join(sorted(_words(rubric[lv]["qualitative_text"]))) for lv in _SCORE_LEVELS if lv in rubric]
        if len(set(texts)) != len(texts):
            errors.append(f'"{kpi.name}": two levels would have the same text')
        new[kpi.name] = rubric
    if len(edits) > MAX_SET_KPIS:
        errors.append(f"at most {MAX_SET_KPIS} KPIs per call")
    if errors:
        return None, "REJECTED (nothing changed): " + "; ".join(errors[:6]) + "."
    new_draft = _replace_guidelines(draft, new)
    by_name = {k.name: k for k in new_draft.kpis}
    lines = [f"Updated {sum(len(v) for v in changed.values())} level(s) in {len(changed)} KPI(s)."]
    for name, lvs in changed.items():
        after = lint_rubric(by_name[name])
        lines.append(
            f"- {name}: levels {', '.join(sorted(set(lvs), key=int))}"
            + ("" if not after else f" (still: {'; '.join(after)})")
        )
    return new_draft, "\n".join(lines)


def view_line(g: GuidelineDraft) -> str:
    """One guideline level for `view_kpis`: the text plus its threshold, if any."""
    crit = g.quantitative_criteria or {}
    if not crit:
        return g.qualitative_text
    unit = f" {crit['unit']}" if crit.get("unit") else ""
    metric = crit.get("metric") or "value"
    return f"{g.qualitative_text} [{metric} {crit.get('operator', '>=')} {crit.get('value')}{unit}]"
