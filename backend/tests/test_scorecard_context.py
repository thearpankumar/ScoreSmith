"""Every AI step of an evaluation is told what its scorecard is about (name, domain, purpose, scope), so a submission
is judged for what that scorecard measures; the AWS image step gets the same context through the job payload."""

from __future__ import annotations

from types import SimpleNamespace

from app.ai.judge import run_judge
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.pipeline.dispatcher import Dispatcher
from app.pipeline.scorecard_context import ScorecardContext
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard
from tests.fakes import (
    FakeAwsJobs,
    FakeBedrockClient,
    FakeJevScoreClient,
    master_converse_fn,
    text_result,
    tool_use_result,
)

NAME = "Billing Support QA"
DOMAIN = "Customer Support"
PURPOSE = "Measure how well support agents resolve billing complaints on the first contact."
SCOPE = "Recorded calls and chat transcripts from the billing queue."


def test_context_from_a_scorecard_is_trimmed_clipped_and_skips_empty_fields() -> None:
    sc = SimpleNamespace(name="  Support QA  ", domain="  ", purpose_statement="x" * 5000, scope=None)
    ctx = ScorecardContext.of(sc)
    assert (ctx.name, ctx.domain, ctx.scope) == ("Support QA", None, None)
    assert len(ctx.purpose) == ScorecardContext.MAX_TEXT
    assert ctx.payload() == {"scorecard": "Support QA", "purpose": ctx.purpose}


def test_prompt_block_names_the_scorecard_and_never_overrides_the_rubric() -> None:
    block = ScorecardContext(NAME, DOMAIN, PURPOSE, SCOPE).prompt_block()
    assert f'"{NAME}"' in block and DOMAIN in block and PURPOSE in block and SCOPE in block
    assert "never overrides the kpi guidelines" in block.lower()
    bare = ScorecardContext(NAME).prompt_block()
    assert "Purpose" not in bare and "Scope" not in bare and "domain" not in bare


async def _seed(db):
    owner, scorecard, version, nodes = await seed_scorecard(db)
    scorecard.name, scorecard.domain, scorecard.purpose_statement, scorecard.scope = NAME, DOMAIN, PURPOSE, SCOPE
    await db.commit()
    return owner, version


def _recording(seen: dict[str, list[str]], **kw) -> FakeBedrockClient:
    """The scoring fakes, also keeping what each tool call was told (system prompt + user prompt)."""
    base = master_converse_fn(**kw)

    def fn(*, messages, system, tools, force_tool_use, model_id):
        name = tools[0].name if tools else None
        seen.setdefault(name, []).append(f"{system or ''}\n{messages[0]['content'][0]['text']}")
        return base(messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id)

    return FakeBedrockClient(converse_fn=fn)


async def _run(aws, bedrock, jev) -> None:
    d = Dispatcher(aws, bedrock, jev, max_concurrent=1, poll_seconds=0.01, jev_retry_delays=(0.0, 0.0),
                   patience_waits=())
    try:
        await d.run_until_idle()
    finally:
        await d.stop()


async def test_every_scoring_step_and_the_aws_job_get_the_scorecard_context(async_db_session) -> None:
    owner, version = await _seed(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    seen: dict[str, list[str]] = {}
    aws, jev = FakeAwsJobs(), FakeJevScoreClient()
    await _run(aws, _recording(seen), jev)

    assert (await get_eval(ev.id)).status == EvaluationStatus.COMPLETED
    # the AWS job carries it for the image and video-frame steps, with the leaf KPI names
    assert aws.start_payloads[0]["context"] == {"scorecard": NAME, "domain": DOMAIN, "purpose": PURPOSE, "scope": SCOPE,
                                                "kpis": ["Innovation", "Execution"]}
    for tool in ("record_identity", "record_digest", "record_evidence", "record_reasoning"):
        assert seen.get(tool), tool
        assert all(NAME in told and PURPOSE in told and SCOPE in told for told in seen[tool]), tool
    # ... and the scoring model reads it as part of its state
    assert jev.calls and all(c["state"]["scorecard"] == {"name": NAME, "domain": DOMAIN, "purpose": PURPOSE,
                                                         "scope": SCOPE} for c in jev.calls)


async def test_the_fallback_judge_gets_the_scorecard_context_too(async_db_session) -> None:
    owner, version = await _seed(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    seen: dict[str, list[str]] = {}
    await _run(FakeAwsJobs(), _recording(seen, judge_score=4), FakeJevScoreClient(fail=True))

    assert (await get_eval(ev.id)).status == EvaluationStatus.COMPLETED
    assert seen.get("record_kpi_judgment")
    assert all(PURPOSE in told for told in seen["record_kpi_judgment"])


async def test_a_manual_judge_run_scores_with_the_scorecard_context(async_db_session) -> None:
    owner, version = await _seed(async_db_session)
    evaluation = Evaluation(scorecard_version_id=version.id, name="Manual run", evaluated_by=owner.id)
    async_db_session.add(evaluation)
    await async_db_session.commit()
    prompts: list[str] = []

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        if not tools:
            return text_result("Evidence: the agent refunded the duplicate charge on the first call.")
        prompts.append(messages[0]["content"][0]["text"])
        return tool_use_result("record_kpi_judgment", {"matched_level": 7, "evidence_quotes": [],
                                                       "reasoning": "Resolved on first contact.", "score": 7})

    await run_judge(async_db_session, evaluation, "The agent refunded the duplicate charge on the first call.",
                    FakeBedrockClient(converse_fn=converse_fn))
    assert prompts and all(NAME in p and PURPOSE in p for p in prompts)
