# ruff: noqa: F811  (pytest fixtures imported from sibling test modules are re-declared as test arguments)
"""More scaling scenarios: racing dispatchers, cancel/retry state machine through the API, idempotency under real
concurrency, worker health-server robustness and the backlog metric's arithmetic."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app import metrics
from app.models.chat_session import ChatSession
from app.models.enums import EvaluationStatus
from app.pipeline.dispatcher import Dispatcher, NotCancellableError, NotRetryableError
from tests.ai_eval_helpers import get_eval, seed_queued, seed_scorecard, wait_status, wait_until
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn
from tests.test_evaluations_ai_api import H, aws, scorecard  # noqa: F401 - fixtures
from tests.test_scaling_api import _items, _post_jobs_keyed

_CREATED: list[Dispatcher] = []


def _dispatcher(aws_, limit: int) -> Dispatcher:
    d = Dispatcher(
        aws_,
        FakeBedrockClient(converse_fn=master_converse_fn()),
        FakeJevScoreClient(),
        max_concurrent=limit,
        poll_seconds=0.01,
        jev_retry_delays=(0.0, 0.0),
        patience_waits=(),
        score_retry_delays=(),
    )
    _CREATED.append(d)
    return d


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    while _CREATED:
        d = _CREATED.pop()
        pending = [t for t in d._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        await d.stop()


# --- racing dispatchers ---------------------------------------------------------------------------------------


async def test_two_dispatchers_ticking_together_never_exceed_the_cluster_wide_cap(async_db_session) -> None:
    """The concurrency limit counts ACTIVE evaluations in the database, so two processes share one budget
    (this is what keeps N workers from multiplying the Bedrock spend)."""
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    evs = await seed_queued(async_db_session, owner, version, 6)
    aws_ = FakeAwsJobs(hold=True)
    d1, d2 = _dispatcher(aws_, 3), _dispatcher(aws_, 3)
    await asyncio.gather(d1.tick(), d2.tick())
    await wait_until(lambda: len(aws_.start_names) == 3)
    await asyncio.sleep(0.2)
    assert len(aws_.start_names) == 3 and len(set(aws_.start_names)) == 3  # not 6, and no duplicates
    rows = [await get_eval(e.id) for e in evs]
    assert sum(1 for r in rows if r.status == EvaluationStatus.QUEUED) == 3
    assert all(r.lease_owner in {d1.worker_id, d2.worker_id} for r in rows if r.status != EvaluationStatus.QUEUED)
    # as the first wave finishes, the freed capacity is claimed by whoever ticks next - each execution once
    first_wave = list(aws_.start_names)
    for name in first_wave:
        aws_.finish(name)
    for name in first_wave:
        await wait_status(name, EvaluationStatus.COMPLETED)
    await asyncio.gather(d1.tick(), d2.tick())
    await wait_until(lambda: len(aws_.start_names) == 6, timeout=30)
    assert sorted(aws_.start_names) == sorted(str(e.id) for e in evs)


async def test_a_dispatcher_at_its_cap_leaves_the_rest_for_later(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    evs = await seed_queued(async_db_session, owner, version, 3)
    aws_ = FakeAwsJobs(hold=True)
    d1, d2 = _dispatcher(aws_, 1), _dispatcher(aws_, 1)
    await d1.tick()
    await wait_until(lambda: len(aws_.start_names) == 1)
    await d1.tick()
    await d2.tick()  # the peer sees the cluster is already at its cap of 1
    await asyncio.sleep(0.2)
    assert len(aws_.start_names) == 1
    statuses = [(await get_eval(e.id)).status for e in evs]
    assert statuses.count(EvaluationStatus.QUEUED) == 2


async def test_fifo_order_is_respected_by_the_claim(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    evs = await seed_queued(async_db_session, owner, version, 4)  # queued_at ascending
    aws_ = FakeAwsJobs(hold=True)
    d = _dispatcher(aws_, 2)
    await d.tick()
    await wait_until(lambda: len(aws_.start_names) == 2)
    assert set(aws_.start_names) == {str(evs[0].id), str(evs[1].id)}


# --- cancel / retry state machine -------------------------------------------------------------------------------


async def test_dispatcher_cancel_and_retry_reject_unknown_and_wrong_state(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    (queued,) = await seed_queued(async_db_session, owner, version, 1)
    (done,) = await seed_queued(async_db_session, owner, version, 1, status=EvaluationStatus.COMPLETED)
    d = _dispatcher(FakeAwsJobs(hold=True), 1)
    with pytest.raises(LookupError):
        await d.cancel(uuid.uuid4())
    with pytest.raises(LookupError):
        await d.retry(uuid.uuid4())
    with pytest.raises(NotCancellableError):
        await d.cancel(done.id)
    with pytest.raises(NotRetryableError):
        await d.retry(queued.id)  # queued evaluations are not "failed"
    with pytest.raises(NotRetryableError):
        await d.retry(done.id)


def test_cancel_retry_through_the_api_follow_the_state_machine(
    client: TestClient,
    seed_user_id: str,
    scorecard,
    aws,  # noqa: F811
) -> None:
    posted = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "sm-1")
    eid = posted.json()["evaluations"][0]["id"]
    h = H(seed_user_id)
    assert client.post(f"/api/v1/evaluations/{eid}/retry", headers=h).status_code == 409  # not failed yet
    cancelled = client.post(f"/api/v1/evaluations/{eid}/cancel", headers=h)
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "failed"
    assert client.post(f"/api/v1/evaluations/{eid}/cancel", headers=h).status_code == 409  # already finished
    retried = client.post(f"/api/v1/evaluations/{eid}/retry", headers=h)
    assert retried.status_code == 202 and retried.json()["status"] == "queued"
    assert client.post(f"/api/v1/evaluations/{eid}/retry", headers=h).status_code == 409  # queued again
    prog = client.get(f"/api/v1/evaluations/{eid}/progress", headers=h).json()
    assert prog["status"] == "queued" and prog["queue_position"] == 1 and prog["error_code"] is None


def test_queue_position_counts_only_older_queued_evaluations(
    client: TestClient,
    seed_user_id: str,
    scorecard,
    aws,  # noqa: F811
) -> None:
    posted = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(3), "qp-1").json()["evaluations"]
    h = H(seed_user_id)
    positions = [
        client.get(f"/api/v1/evaluations/{e['id']}/progress", headers=h).json()["queue_position"] for e in posted
    ]
    assert sorted(positions) == [1, 2, 3]
    # cancelled evaluations leave the line
    client.post(f"/api/v1/evaluations/{posted[0]['id']}/cancel", headers=h)
    assert sorted(
        client.get(f"/api/v1/evaluations/{e['id']}/progress", headers=h).json()["queue_position"] for e in posted[1:]
    ) == [1, 2]


def test_progress_of_an_unknown_evaluation_is_404(client: TestClient, seed_user_id: str) -> None:
    assert client.get(f"/api/v1/evaluations/{uuid.uuid4()}/progress", headers=H(seed_user_id)).status_code == 404
    assert client.post(f"/api/v1/evaluations/{uuid.uuid4()}/cancel", headers=H(seed_user_id)).status_code == 404
    assert client.post(f"/api/v1/evaluations/{uuid.uuid4()}/retry", headers=H(seed_user_id)).status_code == 404


# --- idempotency under real concurrency --------------------------------------------------------------------------


def test_two_simultaneous_posts_with_one_key_create_one_batch(
    client: TestClient,
    seed_user_id: str,
    scorecard,
    aws,  # noqa: F811
) -> None:
    results: list = []
    barrier = threading.Barrier(2)

    def fire() -> None:
        c = type(client)(client.app)  # the auth-translating test client
        barrier.wait()
        results.append(_post_jobs_keyed(c, seed_user_id, scorecard["id"], _items(2), "race-key"))

    threads = [threading.Thread(target=fire) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(r.status_code for r in results) == [202, 202], [r.text for r in results]
    assert results[0].json()["batch_id"] == results[1].json()["batch_id"]
    assert len(client.get("/api/v1/evaluations", headers=H(seed_user_id)).json()) == 2


def test_blank_idempotency_key_is_treated_as_absent(
    client: TestClient,
    seed_user_id: str,
    scorecard,
    aws,  # noqa: F811
) -> None:
    a = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "   ")
    b = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "   ")
    assert a.status_code == b.status_code == 202
    assert a.json()["evaluations"][0]["id"] != b.json()["evaluations"][0]["id"]


def test_the_same_key_text_on_different_endpoints_does_not_collide(
    client: TestClient,
    seed_user_id: str,
    scorecard,
    aws,  # noqa: F811
) -> None:
    jobs = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "shared-key")
    manual = client.post(
        "/api/v1/evaluations",
        json={"scorecard_version_id": scorecard["version_id"], "name": "M"},
        headers={**H(seed_user_id), "Idempotency-Key": "shared-key"},
    )
    assert jobs.status_code == 202 and manual.status_code == 201


# --- worker health server robustness -----------------------------------------------------------------------------


async def _raw(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return raw


async def test_worker_health_server_survives_garbage_and_query_strings(async_db_session) -> None:
    from app.worker import WorkerState, _handle

    state = WorkerState()
    server = await asyncio.start_server(lambda r, w: _handle(r, w, state), host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        junk = await _raw(port, b"\x00\xff garbage\r\n\r\n")
        assert junk == b"" or b"404" in junk or b"200" in junk  # answered or dropped, never crashed
        ok = await _raw(port, b"GET /health?probe=1 HTTP/1.1\r\n\r\n")
        assert b"200 OK" in ok and json.loads(ok.partition(b"\r\n\r\n")[2])["status"] == "ok"
        # the server is still alive after the junk request
        assert b"200 OK" in await _raw(port, b"GET /ready HTTP/1.1\r\n\r\n")
        assert b"404" in await _raw(port, b"GET /admin HTTP/1.1\r\n\r\n")
        assert b"Content-Length" in await _raw(port, b"POST /health HTTP/1.1\r\n\r\n")
    finally:
        server.close()
        await server.wait_closed()


async def test_worker_ready_reports_redis_disabled_without_a_url(async_db_session, monkeypatch) -> None:
    from app.config import get_settings
    from app.worker import WorkerState, _handle

    monkeypatch.setattr(get_settings(), "redis_url", "")
    server = await asyncio.start_server(lambda r, w: _handle(r, w, WorkerState()), host="127.0.0.1", port=0)
    try:
        raw = await _raw(server.sockets[0].getsockname()[1], b"GET /ready HTTP/1.1\r\n\r\n")
        assert json.loads(raw.partition(b"\r\n\r\n")[2])["redis"] == "disabled"
    finally:
        server.close()
        await server.wait_closed()


# --- ready/health on the API ---------------------------------------------------------------------------------------


def test_health_and_ready_need_no_credentials_and_report_role(client: TestClient) -> None:
    for path in ("/health", "/ready"):
        r = client.get(path, headers={"X-Test-Anonymous": "1"})
        assert r.status_code == 200
    body = client.get("/ready").json()
    assert (
        body["role"] in {"api", "worker", "all"}
        and body["database"] == "up"
        and body["redis"] in {"disabled", "up", "down"}
    )
    assert client.get("/health").json() == {"status": "ok"}
    assert "set-cookie" not in client.get("/health").headers


def test_ready_stays_503_with_no_internal_detail_leak(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    class Broken:
        async def __aenter__(self):
            raise OSError("password authentication failed for user qs_app")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(main_mod, "AsyncSessionLocal", Broken)
    r = client.get("/ready")
    assert r.status_code == 503 and "password" not in r.text and "qs_app" not in r.text


def test_ready_is_503_when_the_database_hangs(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    class Slow:
        async def __aenter__(self):
            class Db:
                async def execute(self, *_a, **_k):
                    await asyncio.sleep(30)

            return Db()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(main_mod, "AsyncSessionLocal", Slow)
    assert client.get("/ready").status_code == 503  # bounded by the 2s timeout, not the 30s hang


# --- backlog metric arithmetic -------------------------------------------------------------------------------------


async def test_backlog_with_no_work_is_zero_and_never_divides_by_zero(async_db_session) -> None:
    snap = await metrics.backlog_snapshot()
    assert snap["workers"] == 1 and snap["backlog_per_worker"] == 0


async def test_backlog_worker_count_falls_back_to_distinct_lease_holders(async_db_session) -> None:
    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    evs = await seed_queued(async_db_session, owner, version, 4, status=EvaluationStatus.SCORING)
    for i, ev in enumerate(evs):
        ev.lease_owner = f"worker-{i % 2}"  # two distinct holders
    await async_db_session.commit()
    snap = await metrics.backlog_snapshot()
    assert snap["workers"] == 2 and snap["active_evaluations"] == 4 and snap["backlog_per_worker"] == 2.0


async def test_turns_already_claimed_by_a_worker_are_not_backlog(async_db_session) -> None:
    from datetime import UTC, datetime

    owner, *_ = await seed_scorecard(async_db_session)
    now = datetime.now(UTC)
    async_db_session.add_all(
        [
            ChatSession(user_id=owner.id, pending_turn_started_at=now, turn_message="a", turn_first=True),
            ChatSession(
                user_id=owner.id, pending_turn_started_at=now, turn_message="b", turn_first=True, turn_lease_owner="w1"
            ),
            ChatSession(user_id=owner.id),  # idle session
        ]
    )
    await async_db_session.commit()
    assert (await metrics.backlog_snapshot())["waiting_chat_turns"] == 1


async def test_publish_backlog_metric_never_raises_and_returns_the_snapshot(async_db_session, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "autoscale_metric_namespace", "")
    snap = await metrics.publish_backlog_metric()
    assert snap is not None and snap["backlog_per_worker"] == 0

    async def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(metrics, "backlog_snapshot", boom)
    assert await metrics.publish_backlog_metric() is None  # swallowed: metrics never take a worker down


async def test_set_task_protection_is_a_noop_outside_ecs(monkeypatch) -> None:
    monkeypatch.delenv("ECS_AGENT_URI", raising=False)
    await metrics.set_task_protection(True)  # no network, no error
    monkeypatch.setenv("ECS_AGENT_URI", "http://127.0.0.1:1")  # unreachable agent: best effort, never raises
    await metrics.set_task_protection(True)


async def test_worker_registry_without_redis_is_a_noop(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "redis_url", "")
    await metrics.register_worker("w-x")
    await metrics.unregister_worker("w-x")
    assert await metrics._registered_workers() is None
