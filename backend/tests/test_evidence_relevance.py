"""A KPI is scored on evidence of what IT measures. Found in a live run on a support-desk review: "Average Resolution
Time" scored 9.65 on "Average handle time was 6 minutes 40 seconds" (a different measure), and "Complaint Rate" scored
9.39 on a contact count plus one audited incident, with reasoning that invented "only one complaint". A deliberate
"nothing in the submission measures this" must stay an empty answer instead of turning into keyword matches."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy_utils import Ltree

from app.ai.bedrock_client import ConverseResult
from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.pipeline import graph, master
from app.pipeline.corpus import Corpus, Section
from app.pipeline.dispatcher import Dispatcher
from app.pipeline.jev_scorer import KpiScore, build_score_request
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn, tool_use_result

REVIEW = Corpus("e1", sections=[Section(
    "s001", "src", "review.pdf", "doc", "DOC review.pdf p1",
    "The billing desk handled 4,812 contacts this month. Average handle time was 6 minutes 40 seconds. In one call "
    "the agent misquoted the refund policy; the customer was called back the same day.",
)])


def _kpi(name: str) -> tuple[KpiNode, list[KpiGuideline]]:
    nid = uuid.uuid4()
    node = KpiNode(id=nid, scorecard_version_id=uuid.uuid4(), parent_id=None, path=Ltree(nid.hex), level=1, name=name,
                   weight=100, display_order=0)
    return node, [KpiGuideline(kpi_node_id=nid, score_level=i, qualitative_text=f"{name} level {i}") for i in range(11)]


def _answering(snippets: list, calls: list) -> FakeBedrockClient:
    def fn(*, messages, system, tools, force_tool_use, model_id):
        calls.append({"system": system, "prompt": messages[0]["content"][0]["text"]})
        return ConverseResult(stop_reason="tool_use", tool_name=tools[0].name, tool_input={"snippets": snippets})

    return FakeBedrockClient(converse_fn=fn)


async def test_nothing_measures_the_kpi_is_an_answer_not_a_failure() -> None:
    node, gl = _kpi("Average Resolution Time")
    calls: list = []
    res = await master.select_evidence(_answering([], calls), None, node, gl, REVIEW, {}, None)
    assert res.snippets == [] and not res.used_fallback and res.reason is None
    assert len(calls) == 1  # no "your quotes were not verbatim" retry, no keyword fallback


async def test_invalid_quotes_still_get_one_retry_then_the_keyword_fallback() -> None:
    node, gl = _kpi("Average Resolution Time")
    calls: list = []
    res = await master.select_evidence(
        _answering([{"section_id": "s001", "quote": "resolution time was 2 hours on average"}], calls),
        None, node, gl, REVIEW, {}, None,
    )
    assert len(calls) == 2 and res.used_fallback and res.reason == "no_valid_quotes"


async def test_evidence_steps_only_accept_the_measure_the_kpi_names() -> None:
    node, gl = _kpi("Complaint Rate")
    calls: list = []
    await master.select_evidence(_answering([], calls), None, node, gl, REVIEW, {}, None)
    await master.second_look(_answering([], calls), None, node, gl, REVIEW, [], None)
    for call in calls:
        rule = call["system"].lower()
        assert "different measure" in rule and "handle time is not resolution time" in rule
        assert "return an empty list" in rule


async def test_reasoning_never_derives_figures_the_evidence_does_not_state() -> None:
    node, _gl = _kpi("Complaint Rate")
    seen: list = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        seen.append(system)
        return tool_use_result("record_reasoning", {"reasoning": "No complaint figure is reported."})

    await master.write_reasoning_checked(FakeBedrockClient(converse_fn=fn), None, node, 0, None, [], None)
    assert "never work out a rate, count or total" in seen[0].lower()


def test_scoring_model_relevance_question_rejects_a_different_measure() -> None:
    node, gl = _kpi("Average Resolution Time")
    _state, instructions, _criteria, relevance, _levels = build_score_request(
        node.name, gl[1:], ["Average handle time was 6 minutes 40 seconds"], None
    )
    assert "different measure" in relevance and "different measure" in instructions


async def test_a_kpi_with_no_evidence_scores_zero_for_review_instead_of_on_keyword_matches(async_db_session) -> None:
    owner, _sc, version, _nodes = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    base = master_converse_fn()

    def fn(*, messages, system, tools, force_tool_use, model_id):
        if tools and tools[0].name == "record_evidence":  # the submission measures none of the KPIs
            return tool_use_result("record_evidence", {"snippets": []})
        return base(messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id)

    jev = FakeJevScoreClient()
    bedrock = FakeBedrockClient(converse_fn=fn)
    d = Dispatcher(FakeAwsJobs(), bedrock, jev, max_concurrent=1, poll_seconds=0.01,
                   jev_retry_delays=(0.0, 0.0), patience_waits=())
    try:
        await d.run_until_idle()
    finally:
        await d.stop()

    assert (await get_eval(ev.id)).status == EvaluationStatus.COMPLETED
    assert jev.calls == []  # nothing was handed to the scoring model to "place on the ladder"
    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(
            select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == ev.id))).scalars())
    assert rows and all(float(r.score) == 0 and r.needs_review and r.jev_raw.get("skipped") == "no_evidence"
                        for r in rows)
    # Explained as exactly that. Asking the model to justify a 0 against the level-0 guideline produced "indicating
    # severe understaffing" and "the corpus data is empty": the worst case stated as fact when nothing was reported.
    assert all(r.reasoning_text == graph.NO_EVIDENCE_REASONING for r in rows)
    assert not any(c["tools"] and c["tools"][0].name == "record_reasoning" for c in bedrock.calls)


def test_a_zero_from_the_relevance_gate_is_explained_as_missing_evidence() -> None:
    gated = KpiScore(score=0.0, matched_level=0, needs_review=False, jev_raw={"position": 7.0, "noul": 0.04})
    assert graph._unshown_reasoning(gated) == graph.GATED_REASONING
    real_zero = KpiScore(score=0.0, matched_level=0, needs_review=False, jev_raw={"position": 0.0, "noul": 0.9})
    assert graph._unshown_reasoning(real_zero) is None  # evidence shows the KPI is not met: the model explains it
    scored = KpiScore(score=6.0, matched_level=6, needs_review=False, jev_raw={"position": 5.0, "noul": 0.9})
    assert graph._unshown_reasoning(scored) is None
