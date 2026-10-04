"""LangGraph scoring pipeline (runs locally after the AWS stage has produced `corpus.json`):

    load_corpus -> identify -> digest -> [per KPI, fan-out with Send: evidence -> Jev score -> reasoning]
                -> aggregate

Idempotent: `aggregate` deletes and rewrites every `evaluation_kpi_results` row of the evaluation, so a
restarted/retried scoring run converges to the same state. Each step writes an `evaluation_events` row
and updates `evaluations.stage` (`scoring:<substep>`). Failures surface as `PipelineError(code, msg)`
(codes from docs/ai-eval-contract.md); the dispatcher turns them into a failed evaluation.
"""

from __future__ import annotations

import asyncio
import logging
import operator
import random
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.ai.bedrock_client import BedrockClientProtocol
from app.ai.judge import compute_final_score, effective_leaf_weights, leaf_nodes
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus, rag_band_for_score
from app.models.evaluation import Evaluation
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.evaluation_source import EvaluationSource
from app.models.kpi_node import KpiNode
from app.models.scorecard_version import ScorecardVersion
from app.pipeline import master
from app.pipeline.aws_jobs import AwsJobsProtocol
from app.pipeline.corpus import Corpus, load_corpus
from app.pipeline.events import emit_evaluation_event
from app.pipeline.jev_scorer import JevScoreClientProtocol, score_kpi

logger = logging.getLogger(__name__)

PLACEHOLDER_NAME = "AI evaluation"
JEV_CONCURRENCY = 6


class PipelineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class KpiOutcome:
    kpi_node_id: uuid.UUID
    score: float
    matched_level: int
    reasoning: str
    quotes: list[str]
    needs_review: bool
    jev_raw: dict[str, Any]


class ScoreState(TypedDict, total=False):
    corpus: Corpus
    identity: master.Identity
    digest: dict[str, str]
    results: Annotated[list[KpiOutcome], operator.add]


SECOND_LOOK_MAX_SCORE = 3.5  # a KPI scoring at or below this gets a second, broader search for evidence
SECOND_LOOK_MIN_GAIN = 0.3  # the re-score replaces the first only if it is at least this much higher
SECOND_LOOK_MAX_EVIDENCE = 10


@dataclass
class ScoringDeps:
    aws: AwsJobsProtocol
    bedrock: BedrockClientProtocol
    jev: JevScoreClientProtocol | None
    master_model_id: str | None = None
    fallback_model_id: str | None = None
    jev_retry_delays: tuple[float, ...] | None = None
    # Extra waits (seconds) before retrying a KPI whose master-model call stayed unavailable (throttled) after
    # the call's own retries: long enough for a token-per-minute burst to pass, short enough not to stall a run.
    patience_waits: tuple[float, ...] = (20.0, 45.0)


async def _patiently(call, failed, waits: tuple[float, ...]):
    """Run `call()`; while `failed(result)` is true, wait (jittered) and run it again, once per entry of `waits`."""
    result = await call()
    for wait in waits:
        if not failed(result):
            break
        await asyncio.sleep(wait * (0.7 + 0.6 * random.random()))
        result = await call()
    return result


async def _set_stage(evaluation_id: uuid.UUID, stage: str) -> None:
    try:
        async with AsyncSessionLocal() as db:
            ev = await db.get(Evaluation, evaluation_id)
            if ev is not None and ev.status == EvaluationStatus.SCORING:
                ev.stage = stage
                await db.commit()
    except Exception:  # noqa: BLE001 - stage text is UX only
        logger.warning("could not update stage for %s", evaluation_id, exc_info=True)


@dataclass
class _Loaded:
    direction: str | None
    hint_name: str | None
    hint_email: str | None
    filenames: list[str]
    nodes: list[KpiNode]
    scoring_formula: str | None
    name_is_placeholder: bool


async def _load_evaluation(evaluation_id: uuid.UUID) -> _Loaded:
    async with AsyncSessionLocal() as db:
        ev = await db.get(Evaluation, evaluation_id)
        if ev is None:
            raise PipelineError("internal", "Evaluation no longer exists.")
        sources = (
            await db.execute(select(EvaluationSource).where(EvaluationSource.evaluation_id == evaluation_id))
        ).scalars().all()
        nodes = list(
            (
                await db.execute(
                    select(KpiNode)
                    .where(KpiNode.scorecard_version_id == ev.scorecard_version_id)
                    .options(selectinload(KpiNode.guidelines))
                )
            ).scalars().all()
        )
        version = await db.get(ScorecardVersion, ev.scorecard_version_id)
        return _Loaded(
            direction=ev.direction_prompt,
            hint_name=ev.subject_name,
            hint_email=ev.subject_email,
            filenames=[s.original_name or s.drive_url or "" for s in sources],
            nodes=nodes,
            scoring_formula=version.scoring_formula if version else None,
            name_is_placeholder=ev.name == PLACEHOLDER_NAME,
        )


def _display_name(identity: master.Identity) -> str | None:
    if identity.name and identity.email:
        return f"{identity.name} ({identity.email})"[:255]
    single = identity.name or identity.email
    return single[:255] if single else None


_GENERIC_TITLES = frozenset({"recording", "video", "demo", "screen recording", "screenrecording", "report", "document"})


def _fallback_title(corpus: Any, filenames: list[str]) -> str | None:
    """No submitter found anywhere: name the evaluation after the main file instead of a placeholder.
    Documents come before videos, and a generic stem ("recording") only wins when nothing better exists."""
    sections = list(getattr(corpus, "sections", []))
    ordered = [sec for sec in sections if getattr(sec, "kind", "") == "doc"] + [
        sec for sec in sections if getattr(sec, "kind", "") != "doc"
    ]
    names = [sec.source_name for sec in ordered if getattr(sec, "source_name", None)]
    names += [f for f in filenames if f and not f.lower().startswith("http")]
    stems: list[str] = []
    for raw in names:
        extensions = r"(\.(pdf|docx|md|markdown|txt|mp4|mov|mkv|webm|m4v|pptx))+$"
        stem = re.sub(extensions, "", raw.strip(), flags=re.IGNORECASE)
        stem = re.sub(r"[_\-]+", " ", stem).strip()
        if stem:
            stems.append(stem[:255])
    specific = [x for x in stems if x.lower() not in _GENERIC_TITLES]
    return (specific or stems or [None])[0]


def build_scoring_graph(evaluation_id: uuid.UUID, deps: ScoringDeps, loaded: _Loaded):
    leaves = leaf_nodes(loaded.nodes)
    if not leaves:
        raise PipelineError("scoring_failed", "The scorecard has no KPIs to score.")
    jev_sem = asyncio.Semaphore(JEV_CONCURRENCY)
    done_counter = {"n": 0}
    model = deps.master_model_id

    async def load_corpus_node(_: ScoreState) -> dict[str, Any]:
        await _set_stage(evaluation_id, "scoring:load_corpus")
        corpus = await asyncio.to_thread(load_corpus, deps.aws, str(evaluation_id))
        if corpus is None:
            raise PipelineError("internal", "The extracted corpus (corpus.json) was not found.")
        if corpus.is_empty:
            raise PipelineError("no_content", "No extractable content was found in the submission.")
        await emit_evaluation_event(
            evaluation_id, "scoring_started",
            f"Loaded {len(corpus.sections)} section(s), {corpus.total_chars:,} characters; "
            f"scoring {len(leaves)} KPI(s).",
        )
        return {"corpus": corpus}

    async def identify_node(state: ScoreState) -> dict[str, Any]:
        await _set_stage(evaluation_id, "scoring:identify")
        identity = await master.identify_subject(
            deps.bedrock, model, state["corpus"],
            hint_name=loaded.hint_name, hint_email=loaded.hint_email, filenames=loaded.filenames,
        )
        async with AsyncSessionLocal() as db:
            ev = await db.get(Evaluation, evaluation_id)
            if ev is not None and ev.status == EvaluationStatus.SCORING:
                ev.subject_name = identity.name or ev.subject_name
                ev.subject_email = identity.email or ev.subject_email
                display = _display_name(identity) or _fallback_title(state["corpus"], loaded.filenames)
                if display and loaded.name_is_placeholder:
                    ev.name = display
                await db.commit()
        await emit_evaluation_event(
            evaluation_id, "identified",
            f"Submitter: {identity.name or 'unknown'} <{identity.email or 'no email'}> ({identity.source}).",
        )
        return {"identity": identity}

    async def digest_node(state: ScoreState) -> dict[str, Any]:
        await _set_stage(evaluation_id, "scoring:digest")
        digest = await master.build_digest(deps.bedrock, model, state["corpus"])
        await emit_evaluation_event(evaluation_id, "digest", f"Indexed {len(digest)} section(s).")
        return {"digest": digest}

    def fan_out(state: ScoreState) -> list[Send]:
        return [
            Send("score_kpi", {"kpi_id": leaf.id, "corpus": state["corpus"], "digest": state["digest"]})
            for leaf in leaves
        ]

    leaf_by_id = {leaf.id: leaf for leaf in leaves}

    async def score_kpi_node(payload: dict[str, Any]) -> dict[str, Any]:
        kpi = leaf_by_id[payload["kpi_id"]]
        corpus: Corpus = payload["corpus"]
        media_note = corpus.media_note() or None
        await emit_evaluation_event(evaluation_id, "kpi_evidence", f"Selecting evidence for '{kpi.name}'.")
        ev = await _patiently(
            lambda: master.select_evidence(
                deps.bedrock, model, kpi, kpi.guidelines, corpus, payload["digest"], loaded.direction
            ),
            lambda r: r.reason == "unavailable",
            deps.patience_waits,
        )
        quotes = [s.quote for s in ev.snippets]
        jev_kwargs: dict[str, Any] = {}
        if deps.jev_retry_delays is not None:
            jev_kwargs["retry_delays"] = deps.jev_retry_delays

        async def jev_score(evidence: list[str]):
            async with jev_sem:
                return await score_kpi(
                    deps.jev, deps.bedrock, kpi=kpi, guidelines=kpi.guidelines, evidence=evidence,
                    direction=loaded.direction, fallback_model_id=deps.fallback_model_id,
                    media_note=media_note, **jev_kwargs,
                )

        ks = await jev_score(quotes)

        # Second look. Audits found most wrong LOW scores were evidence the first pass never saw (it quoted the
        # wrong table or page). A low-scoring KPI therefore gets one broader keyword-driven search; the re-score
        # replaces the first only if clearly higher, so a KPI that really has nothing keeps its low score.
        second: dict[str, Any] | None = None
        if ks.score <= SECOND_LOOK_MAX_SCORE and not ks.fallback_reasoning and ev.reason != "unavailable":
            extra = await _patiently(
                lambda: master.second_look(
                    deps.bedrock, model, kpi, kpi.guidelines, corpus, ev.snippets, loaded.direction
                ),
                lambda r: r.reason == "unavailable",
                deps.patience_waits,
            )
            if extra.snippets:
                merged = [*ev.snippets, *extra.snippets][:SECOND_LOOK_MAX_EVIDENCE]
                ks2 = await jev_score([s.quote for s in merged])
                adopted = not ks2.fallback_reasoning and ks2.score >= ks.score + SECOND_LOOK_MIN_GAIN
                second = {
                    "first_score": ks.score, "second_score": ks2.score, "added": len(extra.snippets),
                    "adopted": adopted,
                }
                if adopted:
                    ks = ks2
                    ev = master.EvidenceResult(
                        merged, used_fallback=ev.used_fallback, dropped=ev.dropped, reason=ev.reason
                    )
                    quotes = [s.quote for s in ev.snippets]
            else:
                second = {"first_score": ks.score, "added": 0, "adopted": False}

        level_text = next((g.qualitative_text for g in kpi.guidelines if g.score_level == ks.matched_level), None)
        reasoning_template = False
        if ks.fallback_reasoning:
            reasoning = ks.fallback_reasoning
        else:
            reasoning, reasoning_template = await _patiently(
                lambda: master.write_reasoning_checked(
                    deps.bedrock, model, kpi, ks.score, level_text, ev.snippets, loaded.direction, media_note
                ),
                lambda r: r[1],
                deps.patience_waits,
            )
        # A KPI about what a video shows or says, with a video that could not be analysed, cannot be verified: the
        # score rests on what the document claims about it. Flag it for a human instead of passing it off as checked.
        video_unverifiable = corpus.has_unanalysed_video() and master.is_video_dependent(kpi, kpi.guidelines)
        jev_raw = dict(ks.jev_raw)
        jev_raw["evidence"] = [{"section_id": s.section_id, "label": s.label} for s in ev.snippets]
        if ev.used_fallback:
            jev_raw["evidence_fallback"] = True
        if reasoning_template:
            jev_raw["reasoning_fallback"] = True
        if second is not None:
            jev_raw["second_look"] = second
        if video_unverifiable:
            jev_raw["video_unverifiable"] = True
        needs_review = ks.needs_review or ev.used_fallback or reasoning_template or video_unverifiable
        done_counter["n"] += 1
        await _set_stage(evaluation_id, f"scoring:kpi {done_counter['n']}/{len(leaves)}")
        await emit_evaluation_event(
            evaluation_id, "kpi_scored",
            f"'{kpi.name}': {ks.score:g}/10"
            + (" (second look raised it)" if second and second.get("adopted") else "")
            + (" (needs review)" if needs_review else ""),
        )
        outcome = KpiOutcome(
            kpi_node_id=kpi.id,
            score=ks.score,
            matched_level=ks.matched_level,
            reasoning=reasoning,
            quotes=(ks.fallback_quotes if ks.fallback_reasoning and not quotes else quotes),
            needs_review=needs_review,
            jev_raw=jev_raw,
        )
        return {"results": [outcome]}

    async def aggregate_node(state: ScoreState) -> dict[str, Any]:
        await _set_stage(evaluation_id, "scoring:aggregate")
        outcomes: dict[uuid.UUID, KpiOutcome] = {o.kpi_node_id: o for o in state.get("results", [])}
        if len(outcomes) != len(leaves):
            raise PipelineError("scoring_failed", "Not every KPI produced a score.")
        weights = effective_leaf_weights(loaded.nodes)
        try:
            scores = {k: o.score for k, o in outcomes.items()}
            final = compute_final_score(leaves, scores, weights, loaded.scoring_formula)
        except ValueError as exc:
            raise PipelineError("scoring_failed", str(exc)) from exc
        async with AsyncSessionLocal() as db:
            ev = (
                await db.execute(select(Evaluation).where(Evaluation.id == evaluation_id).with_for_update())
            ).scalar_one_or_none()
            if ev is None or ev.status != EvaluationStatus.SCORING:
                return {}  # cancelled / deleted while scoring: write nothing
            await db.execute(delete(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == evaluation_id))
            for o in outcomes.values():
                db.add(
                    EvaluationKpiResult(
                        evaluation_id=evaluation_id,
                        kpi_node_id=o.kpi_node_id,
                        score=o.score,
                        matched_guideline_level=o.matched_level,
                        reasoning_text=o.reasoning,
                        evidence_quotes=o.quotes,
                        needs_review=o.needs_review,
                        jev_raw=o.jev_raw,
                    )
                )
            now = datetime.now(UTC)
            ev.final_weighted_score = round(final, 2)
            ev.rag_band = rag_band_for_score(final)
            ev.status = EvaluationStatus.COMPLETED
            ev.stage = "done"
            ev.error_code = None
            ev.error_message = None
            ev.submitted_at = now
            ev.finished_at = now
            await db.commit()
        await emit_evaluation_event(evaluation_id, "completed", f"Final score {final:.2f}/10.")
        return {}

    g = StateGraph(ScoreState)
    g.add_node("load_corpus", load_corpus_node)
    g.add_node("identify", identify_node)
    g.add_node("digest", digest_node)
    g.add_node("score_kpi", score_kpi_node)
    g.add_node("aggregate", aggregate_node)
    g.add_edge(START, "load_corpus")
    g.add_edge("load_corpus", "identify")
    g.add_edge("identify", "digest")
    g.add_conditional_edges("digest", fan_out, ["score_kpi"])
    g.add_edge("score_kpi", "aggregate")
    g.add_edge("aggregate", END)
    return g.compile()


async def run_scoring(evaluation_id: uuid.UUID, deps: ScoringDeps) -> None:
    """Runs the whole scoring graph for an evaluation already in status `scoring`. Raises
    `PipelineError` on a business failure; any other exception is wrapped as `scoring_failed`."""
    from app.ai.bedrock_client import BedrockUnavailableError

    settings = get_settings()
    if deps.master_model_id is None:
        deps.master_model_id = settings.bedrock_master_model_id
    if deps.fallback_model_id is None:
        deps.fallback_model_id = settings.bedrock_judge_model_id
    loaded = await _load_evaluation(evaluation_id)
    graph = build_scoring_graph(evaluation_id, deps, loaded)
    try:
        await graph.ainvoke({"results": []})
    except PipelineError:
        raise
    except asyncio.CancelledError:
        raise
    except BedrockUnavailableError as exc:
        raise PipelineError("scoring_failed", f"The AI model was unavailable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("scoring graph failed for evaluation %s", evaluation_id)
        raise PipelineError("scoring_failed", f"Scoring failed: {exc}") from exc
