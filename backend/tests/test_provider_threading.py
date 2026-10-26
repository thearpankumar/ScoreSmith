"""The AI provider (Bedrock / OpenRouter) is resolved once per job and threaded through every call site.

- Evaluations: the Step Functions input always carries `provider`, `evaluations.ai_provider` records it, scoring
  uses the stored provider (or the current one once the stored one is locked) and `retry()` clears it.
- Chat: a worker-claimed turn resolves the current provider's client INSIDE its task; every model id of a turn
  (title, graph, embedding) comes from the turn's own client.
- Concurrency: the AIMD master limiter and the per-call slots are separate per provider.

All clients are fakes: no test reaches Bedrock or OpenRouter."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import providers
from app.ai import scorecard_builder as sb
from app.ai import web_search as web_search_mod
from app.api.v1 import chat as chat_mod
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.deps import get_bedrock_client, get_jev_client, get_web_search_client
from app.main import app
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.scorecard_embedding import ScorecardEmbedding
from app.pipeline import limiter as limiter_mod
from app.pipeline.dispatcher import Dispatcher
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard, wait_execution_recorded, wait_until
from tests.fakes import (
    FakeAwsJobs,
    FakeBedrockClient,
    FakeJevClient,
    FakeJevScoreClient,
    FakeOpenRouterClient,
    full_rubric,
    master_converse_fn,
    text_result,
    tool_use_result,
)
from tests.test_scaling_leases import _chat_row, _queued_turn

OR_MODEL = "openai/gpt-5-mini"  # evaluations (master / judge)
OR_BUILDER_MODEL = "openai/gpt-5.6-luna"  # the chat scorecard builder and its session titles

_CREATED: list[Dispatcher] = []


@pytest.fixture(autouse=True)
async def _drain_dispatchers():
    yield
    while _CREATED:
        d = _CREATED.pop()
        pending = [t for t in d._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        await d.stop()


def make_dispatcher(aws, bedrock) -> Dispatcher:
    d = Dispatcher(
        aws, bedrock, FakeJevScoreClient(), max_concurrent=3, poll_seconds=0.01, jev_retry_delays=(0.0, 0.0),
        patience_waits=(),
    )
    _CREATED.append(d)
    return d


def _master_models(client: FakeBedrockClient) -> set:
    return {c["model_id"] for c in client.calls}


# --- evaluations ---------------------------------------------------------------------------------------------------


async def test_injected_bedrock_client_sends_and_records_bedrock(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    bedrock = FakeBedrockClient(converse_fn=master_converse_fn())
    await make_dispatcher(aws, bedrock).run_until_idle()

    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert aws.start_payloads[0]["provider"] == "bedrock"
    assert done.ai_provider == "bedrock"
    assert _master_models(bedrock) == {get_settings().bedrock_master_model_id}


async def test_injected_openrouter_client_sends_openrouter_and_scores_with_its_models(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    aws = FakeAwsJobs()
    client = FakeOpenRouterClient(converse_fn=master_converse_fn())
    await make_dispatcher(aws, client).run_until_idle()

    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert aws.start_payloads[0]["provider"] == "openrouter"
    assert done.ai_provider == "openrouter"
    assert _master_models(client) == {OR_MODEL}


def _patch_providers(monkeypatch, clients: dict, *, active: dict, available: set[str]) -> None:
    """`active["p"]` is the provider an admin has selected right now (mutable, so a test can switch mid-run)."""

    async def fake_active() -> str:
        return active["p"]

    monkeypatch.setattr(providers, "active_provider", fake_active)
    monkeypatch.setattr(providers, "client_for", lambda p: clients[p])
    monkeypatch.setattr(providers, "is_available", lambda p: p in available)


async def test_unbound_dispatcher_uses_the_active_provider_for_lambda_and_scoring(
    async_db_session, monkeypatch
) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    bedrock = FakeBedrockClient(converse_fn=master_converse_fn())
    openrouter = FakeOpenRouterClient(converse_fn=master_converse_fn())
    _patch_providers(
        monkeypatch, {"bedrock": bedrock, "openrouter": openrouter}, active={"p": "openrouter"},
        available={"bedrock", "openrouter"},
    )
    aws = FakeAwsJobs()
    await make_dispatcher(aws, None).run_until_idle()

    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert aws.start_payloads[0]["provider"] == "openrouter" and done.ai_provider == "openrouter"
    assert openrouter.calls and not bedrock.calls
    assert _master_models(openrouter) == {OR_MODEL}


async def test_a_running_evaluation_finishes_on_the_provider_it_started_with(async_db_session, monkeypatch) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    bedrock = FakeBedrockClient(converse_fn=master_converse_fn())
    openrouter = FakeOpenRouterClient(converse_fn=master_converse_fn())
    active = {"p": "bedrock"}
    _patch_providers(
        monkeypatch, {"bedrock": bedrock, "openrouter": openrouter}, active=active, available={"bedrock", "openrouter"}
    )
    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws, None)
    await d.tick()
    await wait_execution_recorded(ev.id)
    assert aws.start_payloads[0]["provider"] == "bedrock"

    active["p"] = "openrouter"  # an admin switches while the AWS stage is still running
    aws.finish(str(ev.id))
    await d.run_until_idle()

    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert done.ai_provider == "bedrock"
    assert bedrock.calls and not openrouter.calls
    assert _master_models(bedrock) == {get_settings().bedrock_master_model_id}


async def test_scoring_falls_back_to_the_current_provider_when_the_stored_one_is_locked(
    async_db_session, monkeypatch
) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1)
    bedrock = FakeBedrockClient(converse_fn=master_converse_fn())
    openrouter = FakeOpenRouterClient(converse_fn=master_converse_fn())
    active = {"p": "openrouter"}
    available = {"bedrock", "openrouter"}

    async def fake_active() -> str:
        return active["p"]

    monkeypatch.setattr(providers, "active_provider", fake_active)
    monkeypatch.setattr(providers, "client_for", lambda p: {"bedrock": bedrock, "openrouter": openrouter}[p])
    monkeypatch.setattr(providers, "is_available", lambda p: p in available)

    aws = FakeAwsJobs(hold=True)
    d = make_dispatcher(aws, None)
    await d.tick()
    await wait_execution_recorded(ev.id)
    assert (await get_eval(ev.id)).ai_provider == "openrouter"

    available.discard("openrouter")  # the OpenRouter key was removed mid-run
    active["p"] = "bedrock"
    aws.finish(str(ev.id))
    await d.run_until_idle()

    done = await get_eval(ev.id)
    assert done.status == EvaluationStatus.COMPLETED, done.error_message
    assert done.ai_provider == "openrouter"  # what the run started with stays on record
    assert bedrock.calls and not openrouter.calls


async def test_retry_clears_the_recorded_provider(async_db_session, monkeypatch) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (ev,) = await seed_queued(async_db_session, owner, version, 1, status=EvaluationStatus.FAILED)
    async with AsyncSessionLocal() as db:
        row = await db.get(Evaluation, ev.id)
        row.ai_provider = "openrouter"
        row.sfn_execution_arn = "arn:aws:states:us-east-1:123:execution:qs-eval-pipeline:x"
        await db.commit()

    d = make_dispatcher(FakeAwsJobs(), FakeBedrockClient(converse_fn=master_converse_fn()))
    await d.retry(ev.id)
    retried = await get_eval(ev.id)
    assert retried.status == EvaluationStatus.QUEUED
    assert retried.ai_provider is None and retried.sfn_execution_arn is None


def test_get_dispatcher_resolves_the_provider_per_evaluation() -> None:
    from app.pipeline import dispatcher as dispatcher_mod

    dispatcher_mod.reset_dispatcher()
    try:
        assert dispatcher_mod.get_dispatcher().bedrock is None
    finally:
        dispatcher_mod.reset_dispatcher()


# --- chat ----------------------------------------------------------------------------------------------------------


async def test_worker_claim_resolves_the_client_inside_the_turn_task(async_db_session, monkeypatch) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    fake = FakeBedrockClient()
    search = object()
    resolved_in: list[str] = []
    searched_for: list[str] = []
    turns: list[dict] = []

    async def fake_active_client():
        resolved_in.append(asyncio.current_task().get_name())
        return fake

    def fake_web_search_for(provider: str):
        searched_for.append(provider)
        return search

    async def fake_turn(**kw):
        turns.append(kw)

    monkeypatch.setattr(providers, "active_client", fake_active_client)
    monkeypatch.setattr(web_search_mod, "web_search_for", fake_web_search_for)
    monkeypatch.setattr(chat_mod, "_background_turn", fake_turn)

    runner = chat_mod.ChatTurnRunner()
    assert await runner.claim() == 1
    await wait_until(lambda: bool(turns))

    assert resolved_in == [f"chat-turn-{session.id}"]  # inside the spawned task, not in the claim loop
    assert searched_for == ["bedrock"]
    (turn,) = turns
    assert turn["session_id"] == session.id and turn["first_turn"] is True and turn["persist_user_message"] is False
    assert turn["bedrock"] is fake and turn["web_search"] is search and turn["jev"] is not None
    assert providers.models_of(turn["bedrock"]).chat == get_settings().bedrock_chat_model_id


async def test_worker_claim_with_openrouter_active_runs_the_turn_on_openrouter_models(
    async_db_session, monkeypatch
) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    fake = FakeOpenRouterClient()
    seen: dict = {}

    async def fake_active_client():
        return fake

    def fake_title(client, model_id, message):
        seen["title"] = (client, model_id)
        return "OpenRouter title"

    async def fake_start_session(session_id, message, client, *, chat_model_id, **_kw):
        seen["turn"] = (client, chat_model_id)
        raise RuntimeError("stop after the model ids are known")

    monkeypatch.setattr(providers, "active_client", fake_active_client)
    monkeypatch.setattr(web_search_mod, "web_search_for", lambda provider: seen.setdefault("search", provider))
    monkeypatch.setattr(chat_mod, "generate_session_title", fake_title)
    monkeypatch.setattr(chat_mod, "start_session", fake_start_session)

    assert await chat_mod.ChatTurnRunner().claim() == 1

    async def finished() -> bool:
        return (await _chat_row(session.id)).pending_turn_started_at is None

    await wait_until(finished)
    assert seen["search"] == "openrouter"
    assert seen["title"] == (fake, OR_BUILDER_MODEL)
    assert seen["turn"] == (fake, OR_BUILDER_MODEL)
    row = await _chat_row(session.id)
    assert row.title == "OpenRouter title" and row.last_turn_error_code == "turn_failed"


async def test_a_turn_cancelled_while_resolving_its_client_is_recorded(async_db_session, monkeypatch) -> None:
    owner, *_ = await seed_scorecard(async_db_session)
    session = await _queued_turn(async_db_session, owner.id)
    entered = asyncio.Event()

    async def slow_active_client():
        entered.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(providers, "active_client", slow_active_client)
    runner = chat_mod.ChatTurnRunner()
    assert await runner.claim() == 1
    await asyncio.wait_for(entered.wait(), 10)
    await runner.stop()
    row = await _chat_row(session.id)
    assert row.last_turn_error_code == "interrupted" and row.pending_turn_started_at is None


_DRAFT_PATCH = {
    "name": "Support Ticket Quality",
    "purpose": "Rate support ticket resolutions.",
    "domain": "Customer Support",
    "audience": "Support leads",
    "target_score": 8,
    "kpis": [
        {"name": "Accuracy", "weight": 60, "level": 1, "guidelines": full_rubric()},
        {"name": "Tone", "weight": 40, "level": 1, "guidelines": full_rubric()},
    ],
}


@pytest.fixture()
def _chat_overrides():
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()
    yield
    for dep in (get_bedrock_client, get_web_search_client, get_jev_client):
        app.dependency_overrides.pop(dep, None)


def test_inline_chat_turns_use_the_injected_clients_models_end_to_end(
    client: TestClient, db_session: Session, seed_user_id: str, _chat_overrides, monkeypatch
) -> None:
    """With the request's client being OpenRouter, the title, routing, graph and the saved scorecard's embedding
    all use OpenRouter ids (never a Bedrock default): the builder's own model, never the evaluation model."""
    scheduled: list[str] = []
    monkeypatch.setattr(sb, "schedule_backfill", lambda c, *, reason: scheduled.append(reason) or False)
    fake = FakeOpenRouterClient(
        script=[
            text_result("Support Quality"),
            tool_use_result(
                "ask_clarification", {"question": "Purpose?", "options": [], "missing_fields": ["purpose"]}
            ),
            tool_use_result(
                "update_draft", {"patch": _DRAFT_PATCH, "confirmed": True, "assistant_message": "Saved."}
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    user = client.post(
        "/api/v1/users", json={"email": f"or-{uuid.uuid4().hex[:6]}@example.com", "name": "OR"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    first = client.post("/api/v1/chat/sessions", json={"message": "hi"}, headers={"X-User-Id": user["id"]})
    assert first.status_code == 201, first.text
    assert first.json()["title"] == "Support Quality"
    sid = first.json()["session_id"]
    second = client.post(
        f"/api/v1/chat/sessions/{sid}/messages", json={"message": "Rate ticket resolutions."},
        headers={"X-User-Id": user["id"]},
    )
    assert second.status_code == 200, second.text
    version_id = second.json()["materialized_scorecard_version_id"]
    assert version_id

    used = {
        c["model_id"]
        for calls in (fake.calls, fake.route_calls, fake.header_calls, fake.dedup_calls)
        for c in calls
    }
    used.discard(None)  # a call without an explicit id runs on the client's own default model
    assert used == {OR_BUILDER_MODEL}
    assert scheduled == ["check_similarity"]

    models = db_session.execute(
        select(ScorecardEmbedding.embedding_model).where(
            ScorecardEmbedding.scorecard_version_id == uuid.UUID(version_id)
        )
    ).scalars().all()
    assert models == [providers.embedding_model_of(fake)]


# --- concurrency ---------------------------------------------------------------------------------------------------


def test_master_limiter_is_separate_per_provider(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bedrock_max_concurrency", 5)
    monkeypatch.setattr(settings, "openrouter_max_concurrency", 3)
    monkeypatch.setattr(limiter_mod, "_limiters", {})

    bedrock = limiter_mod.get_master_limiter()
    assert limiter_mod.get_master_limiter("bedrock") is bedrock
    openrouter = limiter_mod.get_master_limiter("openrouter")
    assert openrouter is not bedrock
    assert (bedrock.name, bedrock.maximum) == ("master", 5)  # unchanged Redis key for Bedrock
    assert (openrouter.name, openrouter.maximum) == ("master:openrouter", 3)
    assert limiter_mod.get_master_limiter("openrouter") is openrouter


async def test_throttling_one_provider_does_not_slow_the_other(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", "")  # local AIMD only: no shared state leaks between tests
    monkeypatch.setattr(settings, "bedrock_max_concurrency", 8)
    monkeypatch.setattr(settings, "openrouter_max_concurrency", 8)
    monkeypatch.setattr(limiter_mod, "_limiters", {})

    await limiter_mod.get_master_limiter("openrouter").on_throttle()
    assert limiter_mod.get_master_limiter("openrouter").limit == 4
    assert limiter_mod.get_master_limiter("bedrock").limit == 8


async def test_master_calls_use_their_clients_provider_limiter(monkeypatch) -> None:
    from app.pipeline import master

    seen: list[str] = []
    real = limiter_mod.get_master_limiter

    def spy(provider: str = "bedrock"):
        seen.append(provider)
        return real(provider)

    monkeypatch.setattr(get_settings(), "redis_url", "")
    monkeypatch.setattr(master, "get_master_limiter", spy)
    for client in (
        FakeBedrockClient(converse_fn=master_converse_fn()),
        FakeOpenRouterClient(converse_fn=master_converse_fn()),
    ):
        await master._call_tool(client, None, "system", "prompt", master.IDENTIFY_TOOL)
    assert seen == ["bedrock", "openrouter"]


def test_llm_slots_are_named_and_sized_per_provider(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bedrock_max_concurrency", 6)
    monkeypatch.setattr(settings, "openrouter_max_concurrency", 2)
    monkeypatch.setattr(settings, "openrouter_global_concurrency", 0)
    bedrock = sb._bedrock_slots()._sem
    openrouter = sb._bedrock_slots("openrouter")._sem
    assert bedrock.name == "bedrock" and openrouter.name == "openrouter"
    assert bedrock._local_limit() == 6 and openrouter._local_limit() == 2
    assert openrouter._global_limit() == 2  # 0 = the per-process cap


async def test_a_full_provider_slot_does_not_block_the_other_provider(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "bedrock_max_concurrency", 1)
    monkeypatch.setattr(settings, "openrouter_max_concurrency", 1)
    bedrock = FakeBedrockClient(converse_fn=lambda **_kw: text_result("bedrock"))
    openrouter = FakeOpenRouterClient(converse_fn=lambda **_kw: text_result("openrouter"))
    msgs = [{"role": "user", "content": [{"text": "hi"}]}]

    async with sb._bedrock_slots("openrouter"):  # OpenRouter is saturated
        done = await asyncio.wait_for(sb._converse_limited(bedrock, messages=msgs), 5)
        assert done.text == "bedrock"
        blocked = asyncio.create_task(sb._converse_limited(openrouter, messages=msgs))
        await asyncio.sleep(0.1)
        assert not blocked.done()
    assert (await asyncio.wait_for(blocked, 5)).text == "openrouter"
