"""LLM judge helpers shared by the AI evaluation pipeline (`app/pipeline/`).

Per-KPI scores come from Jev (`app/pipeline/jev_scorer.py`). This module keeps:

- the single-call judge (`judge_one_kpi_call`), the Jev scorer's fallback when Jev is unreachable: one Converse call
  with a strict tool schema that forces the ordered shape `matched_level -> evidence_quotes -> reasoning -> score`
  (reasoning-before-score mitigates rate-then-justify bias);
- the weight/scoring math used by the scoring graph and the finalize endpoint.

Only **leaf** KPI nodes are scored directly (an internal Level1-3 node exists purely to group its children — it has
no guidelines, and no WEIGHT, of its own — see migration 0008_category_nodes_no_weight / `app/ai/draft_schema.py`).
The weighted final score is the sum, over every leaf, of `leaf.score * leaf.weight / 100`: the DB's weight-sum
trigger enforces that every LEAF's weight sums to 100 across the WHOLE scorecard version, so the leaves' effective
weights always sum to 1.0 and this is a proper weighted average with no extra normalization.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from app.ai.bedrock_client import BedrockClientProtocol, BedrockUnavailableError, ToolSpec
from app.ai.scoring_formula import FormulaError
from app.ai.scoring_formula import evaluate as evaluate_scoring_formula
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode

JUDGE_TOOL = ToolSpec(
    name="record_kpi_judgment",
    description=(
        "Record your judgment for this single KPI. Fields MUST be produced in this "
        "exact order — matched_level, then evidence_quotes, then reasoning, then score "
        "— so you pick the guideline rung and cite evidence BEFORE writing your "
        "justification and final number (reasoning-before-score mitigates "
        "rate-then-justify bias)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "matched_level": {
                "type": "integer",
                "minimum": 0,
                "maximum": 10,
                "description": (
                    "Which 0-10 guideline rung the input most closely matches. Choose "
                    "this FIRST, before writing evidence_quotes/reasoning/score."
                ),
            },
            "evidence_quotes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Verbatim quoted spans copied exactly from the input text that "
                    "support matched_level. Do not paraphrase or invent quotes."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Written justification, referencing the evidence, for matched_level.",
            },
            "score": {
                "type": "number",
                "minimum": 0,
                "maximum": 10,
                "description": (
                    "Final numeric score (0-10). Normally equals matched_level; may be "
                    "fractional if the input falls between two guideline rungs."
                ),
            },
        },
        "required": ["matched_level", "evidence_quotes", "reasoning", "score"],
    },
)

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict, evidence-grounded quality judge. Score ONLY the single KPI given "
    "to you, against its 0-10 guideline rungs. Every evidence_quotes entry must be a "
    "verbatim substring of the input text — never invent or paraphrase a quote. Call "
    "record_kpi_judgment exactly once."
)


@dataclass
class SingleCallJudgment:
    """Result of one judge call for a KPI."""

    matched_level: int
    evidence_quotes: list[str]
    reasoning: str
    score: float


# --- Pure weight/scoring math (no DB, no Bedrock — directly unit-testable) -------------


def leaf_nodes(nodes: list[KpiNode]) -> list[KpiNode]:
    parent_ids = {n.parent_id for n in nodes if n.parent_id is not None}
    return [n for n in nodes if n.id not in parent_ids]


def effective_leaf_weights(nodes: list[KpiNode]) -> dict[uuid.UUID, float]:
    """A leaf's effective weight is simply its OWN `weight / 100` (see module docstring) —
    category/grouping nodes (any node with children, at any depth) carry no weight of
    their own at all (see migration 0008_category_nodes_no_weight), so there is no
    ancestor chain left to multiply through.

    A leaf with `included_in_scoring=False` (see migration
    0005_scoring_formula_and_kpi_flags) is OMITTED from the returned dict entirely — it is
    tracked/scored (still scored, still gets an `EvaluationKpiResult` row) but contributes
    nothing to, and is not constrained by, the default weighted-average formula. This is
    the one shared definition `compute_weighted_score` (below), the frontend's mirror
    (`lib/kpi-tree.ts::effectiveLeafWeights`), and the DB's own weight-sum trigger all
    agree with."""
    return {
        leaf.id: float(leaf.weight or 0) / 100.0
        for leaf in leaf_nodes(nodes)
        if leaf.included_in_scoring
    }


def compute_weighted_score(
    scores_by_kpi: dict[uuid.UUID, float], effective_weights: dict[uuid.UUID, float]
) -> float:
    """Weighted average of leaf scores. Not clamped — leaf scores are already validated
    0-10 by both the tool schema and the `evaluation_kpi_results` CHECK constraint, and
    effective weights sum to 1.0 for a complete tree, so the result is always in [0, 10].

    This is the DEFAULT formula — byte-for-byte unchanged from before the custom-formula
    feature existed (see `compute_final_score` below for the one new call site that decides
    whether to use this or a custom `scoring_formula` instead)."""
    return sum(scores_by_kpi[kpi_id] * weight for kpi_id, weight in effective_weights.items())


def compute_final_score(
    leaves: list[KpiNode],
    scores_by_kpi_id: dict[uuid.UUID, float],
    effective_weights: dict[uuid.UUID, float],
    scoring_formula: str | None,
) -> float:
    """The ONE place that decides "default weighted-average" vs. "custom scoring_formula"
    — shared by both the AI scoring graph (`app/pipeline/graph.py`) and the manual-evaluation
    finalize endpoint (`POST /evaluations/{id}/finalize` in app/api/v1/evaluations.py), so
    the two paths can never silently disagree about what a scorecard's score means.

    `scoring_formula is None` (every scorecard unless explicitly customized): calls
    `compute_weighted_score` exactly as before — UNCHANGED default behavior, the
    backward-compatibility guarantee this feature must never break.

    A non-null formula: evaluated via `app/ai/scoring_formula.py::evaluate` against every
    LEAF's score keyed by its own KPI name (not just the `included_in_scoring=True` ones —
    a custom formula supersedes the weight-sum mechanism entirely and may deliberately
    reference a KPI that's excluded from the default formula). The result is clamped to
    [0, 10] (`evaluation_kpi_results`/`evaluations.final_weighted_score`'s own DB CHECK
    constraint requires this range; an intentionally exotic formula, e.g. summing two
    KPIs outright, could otherwise exceed it) — a clamp, not a rejection, since the formula
    itself was already syntax/reference-validated before ever being saved (see
    `update_scoring_formula` in scorecard_builder.py and the `validate-formula` endpoint).
    """
    if not scoring_formula:
        return compute_weighted_score(scores_by_kpi_id, effective_weights)

    scores_by_name = {leaf.name: scores_by_kpi_id[leaf.id] for leaf in leaves if leaf.id in scores_by_kpi_id}
    try:
        result = evaluate_scoring_formula(scoring_formula, scores_by_name)
    except FormulaError as exc:
        raise ValueError(f"Custom scoring formula failed to evaluate: {exc}") from exc
    return max(0.0, min(10.0, result))


# --- Bedrock-backed steps ----------------------------------------------------------------


def _format_guidelines(guidelines: list[KpiGuideline]) -> str:
    return "\n".join(
        f"Level {g.score_level}: {g.qualitative_text}"
        + (f" (quantitative criteria: {g.quantitative_criteria})" if g.quantitative_criteria else "")
        for g in guidelines
    )


async def judge_one_kpi_call(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    kpi: KpiNode,
    ordered_guidelines: list[KpiGuideline],
    shared_evidence: str,
    input_text: str,
    *,
    context_block: str = "",
) -> SingleCallJudgment:
    """A single Converse call judging one KPI against its guideline rungs (in the order given).
    `context_block`: what the scorecard is about (app/pipeline/scorecard_context.py), when known."""
    guideline_text = _format_guidelines(ordered_guidelines)
    prompt = (
        (f"{context_block}\n\n" if context_block else "")
        + f"KPI: {kpi.name} (weight {kpi.weight}% of the whole scorecard)\n\n"
        f"Guideline rungs (0-10, listed in no particular order of merit — read every "
        f"rung and match on substance, not position in this list):\n{guideline_text}\n\n"
        f"Shared evidence extracted from the input:\n{shared_evidence}\n\n"
        f"Full input text (for verbatim quoting):\n{input_text}\n\n"
        "Judge ONLY this KPI. Call record_kpi_judgment."
    )
    result = await asyncio.to_thread(
        bedrock.converse,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        system=_JUDGE_SYSTEM_PROMPT,
        tools=[JUDGE_TOOL],
        force_tool_use=True,
        model_id=model_id,
    )
    if not result.is_tool_use or result.tool_input is None:
        raise BedrockUnavailableError(
            f"Judge model did not return a record_kpi_judgment tool call for KPI {kpi.name!r} "
            f"(stop_reason={result.stop_reason!r})"
        )
    data = result.tool_input
    return SingleCallJudgment(
        matched_level=int(data["matched_level"]),
        evidence_quotes=[str(q) for q in data.get("evidence_quotes", [])],
        reasoning=str(data.get("reasoning", "")),
        score=float(data["score"]),
    )
