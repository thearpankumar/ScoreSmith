"""LLM judge: scores every leaf KPI of a scorecard version against a given input text.

Per the plan's judge design: a shared evidence-extraction pre-pass runs **once** per
evaluation (not once per KPI), then, for each leaf KPI, an **ensemble of k=3 independent
Converse calls** (see "Ensemble voting" below), each with a strict tool schema that forces
the ordered shape `matched_level -> evidence_quotes -> reasoning -> score`
(reasoning-before-score is the G-Eval position-bias mitigation, and is also the "basis for
the rating" the product asks for). Every KPI's ensemble runs concurrently with every other
KPI's, and within an ensemble the 3 calls also run concurrently — all via `asyncio.gather`.

Only **leaf** KPI nodes are judged directly (an internal Level1-3 node exists purely to
group its children — it has no guidelines of its own to score against). The weighted
final score is the sum, over every leaf, of `leaf.score * leaf.effective_weight`, where
`effective_weight` is the product of `weight/100` along the full root-to-leaf path — the
correct generalization of "weights sum to 100 per sibling group" to a nested hierarchy
(each level's weight is only relative to its own siblings, so a leaf's true share of the
*whole* scorecard is the product of its own weight fraction and every ancestor's weight
fraction). Because every sibling group in a complete scorecard sums to 100, the leaves'
effective weights always sum to 1.0, so this is a proper weighted average with no
additional normalization needed.

Ensemble voting (k=3, median aggregation): a single LLM-judge call is a noisy point
estimate — both from sampling variance AND from a stable, model-specific position/order
bias in how a rubric's guideline rungs are presented (a *presentation* effect, not
sampling noise, so temperature/resampling alone would not surface it). Running the same
KPI 3 times with the guideline rungs in a different order each time (see
`_order_guidelines` below: ascending, descending, and a deterministic shuffle) and
aggregating by MEDIAN score is a standard, current (2026) LLM-judge-ensemble/self-
consistency mitigation for both effects at once, at k=3 that's cheap enough to afford
given GLM 4.7 Flash's low per-call cost. If the score spread (max-min) across the 3 calls
exceeds 2 points, OR the matched guideline level isn't unanimous across the 3 calls, the
persisted result is flagged `needs_review=True` for a human to look at — disagreement
between 3 independent reads of the same rubric against the same input is itself a signal,
not something to silently average away.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.bedrock_client import BedrockClientProtocol, BedrockUnavailableError, ToolSpec
from app.ai.scoring_formula import FormulaError
from app.ai.scoring_formula import evaluate as evaluate_scoring_formula
from app.models.enums import EvaluationStatus, rag_band_for_score
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard_version import ScorecardVersion

# Number of independent judge calls per leaf KPI, and the score-spread threshold (points,
# on the 0-10 scale) above which a KPI's ensemble result is flagged for human review. A
# non-unanimous matched_level across the k calls also flags for review regardless of spread.
ENSEMBLE_K = 3
REVIEW_SCORE_SPREAD_THRESHOLD = 2.0

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

_EVIDENCE_SYSTEM_PROMPT = (
    "You are a neutral evidence-extraction assistant for a quality-scoring pipeline. "
    "Extract the key factual claims, statements, and notable spans from the given input "
    "as a concise bullet list, preserving exact wording wherever something might later "
    "be quoted as evidence. Do not judge, rate, or score anything — extraction only."
)

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict, evidence-grounded quality judge. Score ONLY the single KPI given "
    "to you, against its 0-10 guideline rungs. Every evidence_quotes entry must be a "
    "verbatim substring of the input text — never invent or paraphrase a quote. Call "
    "record_kpi_judgment exactly once."
)


@dataclass
class SingleCallJudgment:
    """Result of ONE of the k=3 ensemble calls for a KPI (not persisted directly)."""

    matched_level: int
    evidence_quotes: list[str]
    reasoning: str
    score: float


@dataclass
class KpiJudgment:
    """Aggregated ensemble result for one leaf KPI — what actually gets persisted onto
    `evaluation_kpi_results`. `score`/`matched_level`/`evidence_quotes`/`reasoning` are
    taken from the MEDIAN-scoring call among the k=3 (a real, self-consistent judgment
    from one call, rather than an average that could stitch together evidence/reasoning
    that don't actually go together)."""

    kpi_node_id: uuid.UUID
    matched_level: int
    evidence_quotes: list[str]
    reasoning: str
    score: float
    needs_review: bool
    score_variance: float  # spread (max - min) across the k calls, on the 0-10 scale
    raw_calls: list[SingleCallJudgment] = field(default_factory=list)


@dataclass
class JudgeRunResult:
    kpi_judgments: list[KpiJudgment]
    final_weighted_score: float
    rag_band: str


# --- Pure weight/scoring math (no DB, no Bedrock — directly unit-testable) -------------


def leaf_nodes(nodes: list[KpiNode]) -> list[KpiNode]:
    parent_ids = {n.parent_id for n in nodes if n.parent_id is not None}
    return [n for n in nodes if n.id not in parent_ids]


def effective_leaf_weights(nodes: list[KpiNode]) -> dict[uuid.UUID, float]:
    """Root-to-leaf product of weight/100 for every leaf node (see module docstring).

    A leaf with `included_in_scoring=False` (see migration
    0005_scoring_formula_and_kpi_flags) is OMITTED from the returned dict entirely — it is
    tracked/scored (still judged, still gets an `EvaluationKpiResult` row — see
    `run_judge`) but contributes nothing to, and is not constrained by, the default
    weighted-average formula. This is the one shared definition `compute_weighted_score`
    (below), the frontend's mirror (`lib/kpi-tree.ts::effectiveLeafWeights`), and the DB's
    own weight-sum trigger all agree with."""
    by_id = {n.id: n for n in nodes}
    memo: dict[uuid.UUID, float] = {}

    def weight_of(node_id: uuid.UUID) -> float:
        if node_id in memo:
            return memo[node_id]
        node = by_id[node_id]
        own = float(node.weight) / 100.0
        result = own if node.parent_id is None else own * weight_of(node.parent_id)
        memo[node_id] = result
        return result

    return {leaf.id: weight_of(leaf.id) for leaf in leaf_nodes(nodes) if leaf.included_in_scoring}


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
    — shared by both `run_judge` (the AI judge path, below) and the manual-evaluation
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


async def _extract_shared_evidence(
    bedrock: BedrockClientProtocol, input_text: str, model_id: str | None
) -> str:
    result = await asyncio.to_thread(
        bedrock.converse,
        messages=[
            {
                "role": "user",
                "content": [{"text": f"Extract evidence from this input:\n\n{input_text}"}],
            }
        ],
        system=_EVIDENCE_SYSTEM_PROMPT,
        model_id=model_id,
    )
    return result.text or input_text


def _order_guidelines(guidelines: list[KpiGuideline], attempt_index: int, seed_key: str) -> list[KpiGuideline]:
    """Returns `guidelines` in a different rung order per `attempt_index` (0, 1, 2, ...),
    so the k=3 ensemble calls don't all see the identical top-to-bottom rubric
    presentation. Order/position bias is a documented, stable-per-model effect (not
    sampling noise), so this is deliberately about *presentation*, independent of
    whatever temperature/sampling variation the model itself contributes:

    - attempt 0: ascending by score_level (0 -> 10) — the "natural" reading order.
    - attempt 1: descending by score_level (10 -> 0) — the mirror-image presentation.
    - attempt 2+: a deterministic shuffle, seeded off `seed_key` (the KPI id) plus the
      attempt index, so re-running the same evaluation is reproducible but different KPIs
      (and any attempt beyond 2, if ENSEMBLE_K is ever raised) don't all shuffle
      identically.
    """
    ascending = sorted(guidelines, key=lambda g: g.score_level)
    if attempt_index == 0:
        return ascending
    if attempt_index == 1:
        return list(reversed(ascending))
    shuffled = list(ascending)
    random.Random(f"{seed_key}:{attempt_index}").shuffle(shuffled)
    return shuffled


def _format_guidelines(guidelines: list[KpiGuideline]) -> str:
    return "\n".join(
        f"Level {g.score_level}: {g.qualitative_text}"
        + (f" (quantitative criteria: {g.quantitative_criteria})" if g.quantitative_criteria else "")
        for g in guidelines
    )


async def _judge_one_kpi_call(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    kpi: KpiNode,
    ordered_guidelines: list[KpiGuideline],
    shared_evidence: str,
    input_text: str,
) -> SingleCallJudgment:
    """A single Converse call judging one KPI, given guideline rungs in a caller-chosen
    order (see `_order_guidelines`). One of the k=3 ensemble calls made by
    `_judge_kpi_ensemble` below — never called with fewer/more than that context."""
    guideline_text = _format_guidelines(ordered_guidelines)
    prompt = (
        f"KPI: {kpi.name} (weight {kpi.weight}% among its siblings)\n\n"
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


async def _judge_kpi_ensemble(
    bedrock: BedrockClientProtocol,
    model_id: str | None,
    kpi: KpiNode,
    guidelines: list[KpiGuideline],
    shared_evidence: str,
    input_text: str,
) -> KpiJudgment:
    """Runs ENSEMBLE_K independent judge calls for one KPI (guideline order perturbed per
    call — see `_order_guidelines`), concurrently, and aggregates by median score. Flags
    `needs_review` when the calls disagree (score spread > threshold, or matched_level not
    unanimous)."""
    calls = await asyncio.gather(
        *(
            _judge_one_kpi_call(
                bedrock,
                model_id,
                kpi,
                _order_guidelines(guidelines, attempt, str(kpi.id)),
                shared_evidence,
                input_text,
            )
            for attempt in range(ENSEMBLE_K)
        )
    )

    scores = [c.score for c in calls]
    matched_levels = {c.matched_level for c in calls}
    spread = max(scores) - min(scores)
    needs_review = spread > REVIEW_SCORE_SPREAD_THRESHOLD or len(matched_levels) > 1

    median_score = statistics.median(scores)
    # The representative call is the one whose OWN score is closest to the median (for an
    # odd k this is exactly the middle call when sorted by score) — keeps evidence_quotes/
    # reasoning/matched_level internally consistent with each other (they come from one
    # real model turn), rather than averaging fields that don't average meaningfully.
    representative = min(calls, key=lambda c: abs(c.score - median_score))

    return KpiJudgment(
        kpi_node_id=kpi.id,
        matched_level=representative.matched_level,
        evidence_quotes=representative.evidence_quotes,
        reasoning=representative.reasoning,
        score=median_score,
        needs_review=needs_review,
        score_variance=spread,
        raw_calls=list(calls),
    )


async def _load_kpi_nodes(db: AsyncSession, scorecard_version_id: uuid.UUID) -> list[KpiNode]:
    result = await db.execute(
        select(KpiNode)
        .where(KpiNode.scorecard_version_id == scorecard_version_id)
        .options(selectinload(KpiNode.guidelines))
    )
    return list(result.scalars().all())


async def run_judge(
    db: AsyncSession,
    evaluation: Evaluation,
    input_text: str,
    bedrock: BedrockClientProtocol,
    judge_model_id: str | None = None,
) -> JudgeRunResult:
    """Runs the full judge pipeline for `evaluation` (already persisted, pointing at some
    `scorecard_version_id`) against `input_text`, and persists per-KPI results + the
    evaluation's final score/band via the existing SQLAlchemy models (no raw SQL)."""
    nodes = await _load_kpi_nodes(db, evaluation.scorecard_version_id)
    leaves = leaf_nodes(nodes)
    if not leaves:
        raise ValueError(f"scorecard_version {evaluation.scorecard_version_id} has no KPI nodes to judge.")
    weights = effective_leaf_weights(nodes)

    evaluation.status = EvaluationStatus.IN_PROGRESS
    await db.flush()

    shared_evidence = await _extract_shared_evidence(bedrock, input_text, judge_model_id)

    # Every leaf's k=3-call ensemble runs concurrently with every other leaf's (each
    # ensemble internally gathers its own k calls too — see _judge_kpi_ensemble).
    judgments = await asyncio.gather(
        *(
            _judge_kpi_ensemble(bedrock, judge_model_id, leaf, leaf.guidelines, shared_evidence, input_text)
            for leaf in leaves
        )
    )

    version = await db.get(ScorecardVersion, evaluation.scorecard_version_id)
    scoring_formula = version.scoring_formula if version is not None else None

    scores_by_kpi = {j.kpi_node_id: j.score for j in judgments}
    final_score = compute_final_score(leaves, scores_by_kpi, weights, scoring_formula)
    band = rag_band_for_score(final_score)

    for judgment in judgments:
        db.add(
            EvaluationKpiResult(
                evaluation_id=evaluation.id,
                kpi_node_id=judgment.kpi_node_id,
                score=judgment.score,
                matched_guideline_level=judgment.matched_level,
                reasoning_text=judgment.reasoning,
                evidence_quotes=judgment.evidence_quotes,
                needs_review=judgment.needs_review,
                score_variance=judgment.score_variance,
                ensemble_raw_scores=[
                    {"matched_level": c.matched_level, "score": c.score} for c in judgment.raw_calls
                ],
            )
        )

    evaluation.final_weighted_score = round(final_score, 2)
    evaluation.rag_band = band
    evaluation.status = EvaluationStatus.COMPLETED
    evaluation.submitted_at = datetime.now(UTC)

    await db.commit()

    return JudgeRunResult(
        kpi_judgments=list(judgments), final_weighted_score=final_score, rag_band=band.value
    )
