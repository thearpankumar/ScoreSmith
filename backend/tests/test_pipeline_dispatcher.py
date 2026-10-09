"""Dispatcher + scoring-graph integration tests (real Postgres, fake AWS / Bedrock / Jev)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.ai.bedrock_client import BedrockUnavailableError
from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_event import EvaluationEvent
from app.models.evaluation_kpi_result import EvaluationKpiResult
from app.pipeline.dispatcher import Dispatcher, NotCancellableError, NotRetryableError
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard, wait_status, wait_until
from tests.fakes import DEFAULT_QUOTE, FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn

_CREATED: list[Dispatcher] = []


@pytest.fixture(autouse=True)
async def _drain_dispatchers():
    """A driver task still mid-transaction when the test's event loop closes would leave a pooled
    connection `idle in transaction` and block the next test's TRUNCATE: let drivers finish, then stop."""
    yield
    while _CREATED:
        d = _CREATED.pop()
        pending = [t for t in d._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        await d.stop()


def make_dispatcher(aws, *, jev=None, bedrock=None, limit=3):
    d = _make_dispatcher(aws, jev=jev, bedrock=bedrock, limit=limit)
    _CREATED.append(d)
    return d


def _make_dispatcher(aws, *, jev=None, bedrock=None, limit=3):
    return Dispatcher(
        aws,
        bedrock or FakeBedrockClient(converse_fn=master_converse_fn()),
        jev if jev is not None else FakeJevScoreClient(),
        max_concurrent=limit,
        poll_seconds=0.01,
        jev_retry_delays=(0.0, 0.0),
        patience_waits=(),
    )


async def results_for(eid):
    async with AsyncSessionLocal() as db:
        return list(
            (await db.execute(select(EvaluationKpiResult).where(EvaluationKpiResult.evaluation_id == eid))).scalars()
        )


async def test_full_run_scores_with_jev_and_names_evaluation(async_db_session) -> None:
    owner, _sc, version, nodes = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    d = make_dispatcher(aws)
    await d.run_until_idle()

    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert done.stage == "done" and done.finished_at is not None
    assert done.subject_email == "jane@example.com" and done.subject_name == "Jane Doe"
    assert done.name == "Jane Doe (jane@example.com)"
    # Jev position 6.0 -> score 7 on both KPIs (guideline levels 1-10, score = position + 1).
    assert float(done.final_weighted_score) == 7.0 and done.rag_band.value == "band_7"
    rows = await results_for(ev.id)
    assert len(rows) == 2
    for r in rows:
        assert float(r.score) == 7.0 and r.matched_guideline_level == 7 and r.needs_review is False
        assert r.evidence_quotes == [DEFAULT_QUOTE]
        assert r.jev_raw["position"] == 6.0 and r.jev_raw["noul"] == 0.9 and "probabilities" in r.jev_raw
        assert r.reasoning_text
    # The AWS payload followed the contract.
    payload = aws.start_payloads[0]
    assert payload["evaluation_id"] == str(ev.id) and payload["sources"][0]["kind"] == "upload"
    assert aws.start_names == [str(ev.id)]
    async with AsyncSessionLocal() as db:
        types = {e.event_type for e in (await db.execute(select(EvaluationEvent))).scalars()}
    assert {"claimed", "ingest_started", "scoring_started", "identified", "kpi_scored", "completed"} <= types


async def test_dispatcher_runs_fifo_with_limit_2_and_starts_next_when_one_finishes(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    evs = await seed_queued(async_db_session, owner, version, 5)
    ids = [str(e.id) for e in evs]
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws, limit=2)

    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 2)
    # FIFO = the two OLDEST were claimed. The two drivers call StartExecution from worker threads, so the order of
    # those two calls among themselves is not defined; the claim order is what FIFO guarantees.
    assert set(aws.start_names) == set(ids[:2])
    await d.tick()  # no free capacity
    await asyncio.sleep(0.1)
    assert set(aws.start_names) == set(ids[:2])
    assert (await get_eval(ids[2])).status == EvaluationStatus.QUEUED

    aws.finish(ids[0])
    await wait_status(ids[0], EvaluationStatus.COMPLETED)
    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 3)
    assert aws.start_names[2] == ids[2]

    # The remaining ones drain in order as slots free up.
    for eid in ids[1:]:

        async def started(eid=eid) -> bool:
            await d.tick()
            return eid in aws.start_names

        await wait_until(started)
        aws.finish(eid)
        await wait_status(eid, EvaluationStatus.COMPLETED)
    await d.run_until_idle()
    assert set(aws.start_names[:2]) == set(ids[:2]) and aws.start_names[2:] == ids[2:]
    async with AsyncSessionLocal() as db:
        n_done = (
            await db.execute(
                select(func.count()).select_from(Evaluation).where(Evaluation.status == EvaluationStatus.COMPLETED)
            )
        ).scalar_one()
    assert n_done == 5


async def test_concurrent_ticks_never_exceed_limit(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    await seed_queued(async_db_session, owner, version, 4)
    aws = FakeAwsJobs(hold=True)
    d1, d2 = make_dispatcher(aws, limit=1), make_dispatcher(aws, limit=1)
    results = await asyncio.gather(d1.tick(), d2.tick(), d1.tick(), d2.tick())
    assert sum(len(r) for r in results) == 1
    await wait_until(lambda: len(aws.start_names) == 1)
    await asyncio.sleep(0.1)
    assert len(aws.start_names) == 1
    async with AsyncSessionLocal() as db:
        active = (
            await db.execute(
                select(func.count()).select_from(Evaluation).where(Evaluation.status == EvaluationStatus.INGESTING)
            )
        ).scalar_one()
    assert active == 1
    await d1.stop()


async def test_limit_is_clamped_1_to_5() -> None:
    assert make_dispatcher(FakeAwsJobs(), limit=99).limit == 5
    assert make_dispatcher(FakeAwsJobs(), limit=0).limit == 1


async def test_cancel_queued_and_running_evaluations(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    evs = await seed_queued(async_db_session, owner, version, 2)
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws, limit=1)
    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    running, queued = evs

    await d.cancel(queued.id)  # queued: no AWS call
    q = await get_eval(queued.id)
    assert q.status == EvaluationStatus.FAILED and q.error_code == "cancelled" and not aws.stopped

    await d.cancel(running.id)  # running: StopExecution + driver cancelled
    r = await get_eval(running.id)
    assert r.status == EvaluationStatus.FAILED and r.error_code == "cancelled"
    assert len(aws.stopped) == 1
    await wait_until(lambda: not d._tasks)

    with pytest.raises(NotCancellableError):
        await d.cancel(running.id)
    with pytest.raises(LookupError):
        import uuid

        await d.cancel(uuid.uuid4())
    # Cancelling frees capacity: nothing queued remains, and the cancelled run never reaches scoring.
    assert await results_for(running.id) == []


async def test_retry_requeues_with_attempt_suffix_in_execution_name(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws)
    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    aws.finish(str(ev.id), "FAILED", error_code="drive_inaccessible", error_message="Folder is private.")
    failed = await wait_status(ev.id, EvaluationStatus.FAILED)
    assert failed.error_code == "drive_inaccessible" and failed.error_message == "Folder is private."

    await d.retry(ev.id)
    again = await get_eval(ev.id)
    assert again.status == EvaluationStatus.QUEUED and again.attempt == 2 and again.error_code is None
    assert again.sfn_execution_arn is None

    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 2)
    assert aws.start_names == [str(ev.id), f"{ev.id}-a2"]
    aws.finish(str(ev.id))
    done = await wait_status(ev.id, EvaluationStatus.COMPLETED)
    assert done.error_code is None
    with pytest.raises(NotRetryableError):
        await d.retry(ev.id)


async def test_recovery_resumes_by_execution_arn_without_restarting(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ingesting, scoring) = await seed_queued(async_db_session, owner, version, 2)
    aws = FakeAwsJobs(hold=True)
    arn = "arn:aws:states:us-east-1:123:execution:qs-eval-pipeline:" + str(ingesting.id)
    aws.executions[arn] = {"status": "SUCCEEDED", "payload": {"evaluation_id": str(ingesting.id)}, "name": "x"}
    from tests.fakes import default_corpus

    aws.json_objects[f"derived/{ingesting.id}/corpus.json"] = default_corpus(str(ingesting.id))
    aws.json_objects[f"derived/{scoring.id}/corpus.json"] = default_corpus(str(scoring.id))
    async with AsyncSessionLocal() as db:
        a = await db.get(Evaluation, ingesting.id)
        a.status, a.sfn_execution_arn = EvaluationStatus.INGESTING, arn
        b = await db.get(Evaluation, scoring.id)
        b.status = EvaluationStatus.SCORING
        await db.commit()

    d = make_dispatcher(aws)  # a "restarted" process: no drivers yet
    assert await d.recover() == 2
    await wait_status(ingesting.id, EvaluationStatus.COMPLETED)
    await wait_status(scoring.id, EvaluationStatus.COMPLETED)
    assert aws.start_names == []  # resumed, never restarted a new execution
    assert len(await results_for(scoring.id)) == 2


async def test_scoring_is_idempotent_rewrites_results(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    await make_dispatcher(aws).run_until_idle()
    first_ids = {r.id for r in await results_for(ev.id)}
    async with AsyncSessionLocal() as db:
        (await db.get(Evaluation, ev.id)).status = EvaluationStatus.SCORING
        await db.commit()
    await make_dispatcher(aws).run_until_idle()
    second = await results_for(ev.id)
    assert len(second) == 2 and first_ids.isdisjoint({r.id for r in second})


async def test_empty_corpus_fails_with_no_content(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = {
        "evaluation_id": str(ev.id), "stats": {}, "warnings": [],
        "sections": [
            {"id": "s001", "source_id": "a", "source_name": "a.pdf", "kind": "doc", "label": "DOC a.pdf", "text": "  "}
        ],
    }
    await make_dispatcher(aws).run_until_idle()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.FAILED and done.error_code == "no_content"
    assert await results_for(ev.id) == []


@pytest.mark.parametrize(
    ("outcome", "status_code", "expected"),
    [
        ("FAILED", "extract_failed", "extract_failed"),
        ("FAILED", "bogus_code", "internal"),
        ("FAILED", None, "internal"),
        ("TIMED_OUT", None, "timeout"),
        ("ABORTED", None, "cancelled"),
    ],
)
async def test_aws_failures_map_to_error_codes(async_db_session, outcome, status_code, expected) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws)
    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    aws.finish(str(ev.id), outcome, error_code=status_code, error_message="boom")
    done = await wait_status(ev.id, EvaluationStatus.FAILED)
    assert done.error_code == expected


async def test_progress_json_is_mirrored_into_evaluation_and_sources(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws)
    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    src_id = aws.start_payloads[0]["sources"][0]["source_id"]
    aws.set_progress(
        str(ev.id),
        {
            "stage": "extract", "message": "Extracting report.pdf",
            "files": [{"source_id": src_id, "name": "report.pdf", "state": "running", "detail": ""}],
            "counters": {"files_total": 1, "files_done": 0},
        },
    )
    await wait_until(lambda: _stage_is(ev.id, "extract"))
    cur = await get_eval(ev.id)
    assert cur.progress["counters"]["files_total"] == 1
    aws.finish(str(ev.id))
    await wait_status(ev.id, EvaluationStatus.COMPLETED)


async def _stage_is(eid, stage) -> bool:
    return (await get_eval(eid)).stage == stage


async def test_jev_outage_falls_back_to_glm_and_flags_needs_review(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    jev = FakeJevScoreClient(fail=True)
    bedrock = FakeBedrockClient(converse_fn=master_converse_fn(judge_score=4))
    await make_dispatcher(FakeAwsJobs(), jev=jev, bedrock=bedrock).run_until_idle()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED and float(done.final_weighted_score) == 4.0
    assert len(jev.calls) == 2 * 3  # 2 KPIs x (1 attempt + 2 retries)
    for r in await results_for(ev.id):
        assert r.needs_review is True and r.jev_raw["fallback"] == "glm_single_call"
        assert r.reasoning_text == "GLM fallback."


async def test_bedrock_outage_uses_deterministic_fallbacks_and_flags_review(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)

    def down(**_kw):
        raise BedrockUnavailableError("bedrock down")

    bedrock = FakeBedrockClient(converse_fn=down)
    await make_dispatcher(FakeAwsJobs(), bedrock=bedrock).run_until_idle()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert done.subject_email == "jane@example.com"  # regex fallback
    for r in await results_for(ev.id):
        assert r.needs_review is True and r.jev_raw.get("evidence_fallback") is True and r.reasoning_text


async def test_jev_and_bedrock_both_down_fails_scoring_failed(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)

    def down(**_kw):
        raise BedrockUnavailableError("bedrock down")

    d = make_dispatcher(FakeAwsJobs(), jev=FakeJevScoreClient(fail=True), bedrock=FakeBedrockClient(converse_fn=down))
    await d.run_until_idle()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.FAILED and done.error_code == "scoring_failed"


async def test_noul_gate_scores_zero_in_pipeline(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    await make_dispatcher(FakeAwsJobs(), jev=FakeJevScoreClient(position=8.0, noul=0.05)).run_until_idle()
    done = await get_eval(ev.id)
    assert float(done.final_weighted_score) == 0.0
    assert all(float(r.score) == 0.0 for r in await results_for(ev.id))


async def test_start_execution_failure_marks_internal_error(async_db_session) -> None:
    from app.pipeline.aws_jobs import AwsNotConfiguredError

    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.fail_start = AwsNotConfiguredError("SFN_STATE_MACHINE_ARN is not configured.")
    await make_dispatcher(aws).run_until_idle()
    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.FAILED and done.error_code == "internal"


# --- source rows must follow the files they expand into (found by auditing the database) ------------------


def test_roll_up_maps_drive_expanded_files_to_their_source_row():
    from app.pipeline.dispatcher import roll_up_source_states

    sid = "11111111-1111-1111-1111-111111111111"
    upload = "22222222-2222-2222-2222-222222222222"
    files = [
        {"source_id": f"{sid}-01", "name": "demo.mp4", "state": "done", "detail": ""},
        {"source_id": f"{sid}-02", "name": "report.pdf", "state": "done", "detail": ""},
        {"source_id": f"{sid}-03", "name": "code.zip", "state": "skipped", "detail": "Unsupported file type: code.zip"},
        {"source_id": upload, "name": "a.pdf", "state": "failed", "detail": "not a valid PDF"},
        {"source_id": "unrelated-id", "name": "x", "state": "done", "detail": ""},
    ]
    rolled = roll_up_source_states(files, {sid, upload})
    assert rolled[sid] == ("done", ["Unsupported file type: code.zip"])  # processed files win over a skipped one
    assert rolled[upload] == ("failed", ["not a valid PDF"])
    assert set(rolled) == {sid, upload}


def test_roll_up_states_priority_and_parent_id():
    from app.pipeline.dispatcher import roll_up_source_states

    sid = "33333333-3333-3333-3333-333333333333"

    def one(*states):
        files = [{"source_id": f"{sid}-{i:02d}", "state": s, "parent_source_id": sid} for i, s in enumerate(states, 1)]
        return roll_up_source_states(files, {sid})[sid][0]

    assert one("done", "running") == "running"
    assert one("done", "pending") == "pending"
    assert one("failed", "skipped") == "failed"
    assert one("skipped", "skipped") == "skipped"
    assert one("failed", "done") == "done"


async def test_failed_evaluation_leaves_no_pending_sources(async_db_session) -> None:
    """Found by auditing the database: a failed evaluation (e.g. a private Drive link) kept its source rows
    'pending' forever."""
    from app.models.evaluation_source import EvaluationSource

    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws)
    await d.tick()
    await wait_until(lambda: len(aws.start_names) == 1)
    aws.finish(str(ev.id), "FAILED", error_code="drive_inaccessible", error_message="not public")
    await wait_status(ev.id, EvaluationStatus.FAILED)
    async with AsyncSessionLocal() as db:
        query = select(EvaluationSource).where(EvaluationSource.evaluation_id == ev.id)
        rows = (await db.execute(query)).scalars().all()
    assert rows and all(r.status == "failed" for r in rows)
    assert all(r.warnings for r in rows)


# --- second look and video verifiability, end to end through the scoring graph ----------------------------------


def _cov_corpus(eid) -> dict:
    return {
        "evaluation_id": str(eid), "stats": {}, "warnings": [],
        "sections": [
            {"id": "s001", "source_id": "a", "source_name": "a.pdf", "kind": "doc", "label": "DOC a.pdf p1",
             "text": f"FleetPulse by Jane Doe. {DEFAULT_QUOTE}. A non-functional table lists targets."},
            {"id": "s002", "source_id": "a", "source_name": "a.pdf", "kind": "doc", "label": "DOC a.pdf p2",
             "text": "Unit test coverage reached 91 percent on the domain layer with an 80 percent gate in CI."},
        ],
    }


class _StagedJev(FakeJevScoreClient):
    """Low unless the evidence mentions the missed fact; `always_low` models a KPI that truly has nothing."""

    def __init__(self, *, always_low: bool = False) -> None:
        super().__init__(position=0.5)
        self.always_low = always_low

    async def score(self, *, state, instructions, criteria, relevance_instructions):
        self.position = 0.5 if self.always_low or "91 percent" not in str(state.get("evidence")) else 7.0
        return await super().score(
            state=state, instructions=instructions, criteria=criteria, relevance_instructions=relevance_instructions
        )


def _second_look_bedrock(prompts: list[str]):
    from tests.fakes import tool_use_result

    base = master_converse_fn(DEFAULT_QUOTE)

    def fn(*, messages, system, tools, force_tool_use, model_id):
        text = messages[0]["content"][0]["text"]
        if tools and tools[0].name == "record_evidence" and "Evidence already found" in text:
            prompts.append(text)
            quote = "Unit test coverage reached 91 percent on the domain layer"
            return tool_use_result("record_evidence", {"snippets": [{"section_id": "s002", "quote": quote}]})
        return base(
            messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
        )

    return FakeBedrockClient(converse_fn=fn)


async def test_second_look_raises_a_low_score_when_it_finds_evidence_the_first_pass_missed(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session, kpi_names=("Unit test coverage",))
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = _cov_corpus(ev.id)
    prompts: list[str] = []
    await make_dispatcher(aws, jev=_StagedJev(), bedrock=_second_look_bedrock(prompts)).run_until_idle()

    (row,) = await results_for(ev.id)
    assert len(prompts) == 1
    assert float(row.score) == 8.0 and row.matched_guideline_level == 8  # position 7 -> score 8
    assert row.jev_raw["second_look"] == {"first_score": 1.5, "second_score": 8.0, "added": 1, "adopted": True}
    assert "Unit test coverage reached 91 percent on the domain layer" in row.evidence_quotes
    assert DEFAULT_QUOTE in row.evidence_quotes and row.needs_review is False


async def test_second_look_keeps_the_low_score_when_the_extra_evidence_changes_nothing(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session, kpi_names=("Unit test coverage",))
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = _cov_corpus(ev.id)
    await make_dispatcher(aws, jev=_StagedJev(always_low=True), bedrock=_second_look_bedrock([])).run_until_idle()

    (row,) = await results_for(ev.id)
    assert float(row.score) == 1.5
    assert row.jev_raw["second_look"]["adopted"] is False and row.jev_raw["second_look"]["added"] == 1
    assert row.evidence_quotes == [DEFAULT_QUOTE]  # the unhelpful extra quote is not attached


async def test_second_look_is_skipped_for_kpis_that_already_score_well(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session, kpi_names=("Unit test coverage",))
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = _cov_corpus(ev.id)
    prompts: list[str] = []
    bedrock = _second_look_bedrock(prompts)
    await make_dispatcher(aws, jev=FakeJevScoreClient(position=6.0), bedrock=bedrock).run_until_idle()

    (row,) = await results_for(ev.id)
    assert prompts == [] and "second_look" not in row.jev_raw and float(row.score) == 7.0


async def test_video_kpi_is_flagged_when_the_video_could_not_be_analysed(async_db_session) -> None:
    from sqlalchemy import select as sa_select

    from app.models.kpi_node import KpiNode

    owner, _sc, version, nodes = await seed_scorecard(
        async_db_session, kpi_names=("The live demo shows the system running", "API latency is measured")
    )
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    aws.json_objects[f"derived/{ev.id}/corpus.json"] = _cov_corpus(ev.id)
    aws.json_objects[f"derived/{ev.id}/manifest.json"] = {
        "files": [{"source_id": "v1", "kind": "video", "original_name": "demo.mp4", "status": "ok"}]
    }
    aws.json_objects[f"derived/{ev.id}/video/v1/plan.json"] = {
        "source": "demo.mp4", "duration_sec": 81.5, "has_audio": False
    }
    jev = FakeJevScoreClient(position=6.0)
    await make_dispatcher(aws, jev=jev).run_until_idle()

    async with AsyncSessionLocal() as db:
        mine = {x.id for x in nodes}
        names = {n.id: n.name for n in (await db.execute(sa_select(KpiNode))).scalars() if n.id in mine}
    by_name = {names[r.kpi_node_id]: r for r in await results_for(ev.id)}
    demo = by_name["The live demo shows the system running"]
    latency = by_name["API latency is measured"]
    assert demo.needs_review is True and demo.jev_raw["video_unverifiable"] is True
    assert latency.needs_review is False and "video_unverifiable" not in latency.jev_raw
    # every Jev request told the scorer that the video could not be analysed
    assert jev.calls and all("silent" in call["state"]["submission_media"] for call in jev.calls)
