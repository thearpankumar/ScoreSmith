"""Idempotency-Key, readiness, JSON logs, role handling and the worker's health server."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.logging_config import JsonFormatter, bind_log_context, log_context
from tests.test_evaluations_ai_api import (  # noqa: F401 - fixtures
    DRIVE,
    H,
    aws,
    make_scorecard,
    post_jobs,
    scorecard,
)

# --- Idempotency-Key ----------------------------------------------------------------------------------------


def _items(n: int = 1) -> list[dict]:
    return [
        {"subject_email": f"p{i}@x.com", "sources": [{"kind": "drive", "drive_url": f"{DRIVE}{i}"}]} for i in range(n)
    ]


def _post_jobs_keyed(client, uid, scorecard_id, items, key):
    return client.post(
        "/api/v1/evaluations/ai/jobs", headers={**H(uid), "Idempotency-Key": key},
        json={"scorecard_id": scorecard_id, "direction_prompt": None, "items": items},
    )


def test_replayed_job_post_returns_the_same_evaluations_and_creates_nothing_new(
    client: TestClient, seed_user_id: str, scorecard, aws  # noqa: F811
) -> None:
    first = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(3), "key-batch-1")
    assert first.status_code == 202, first.text
    second = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(3), "key-batch-1")
    assert second.status_code == 202, second.text
    a, b = first.json(), second.json()
    assert a["batch_id"] and b["batch_id"] == a["batch_id"]
    assert [e["id"] for e in b["evaluations"]] == [e["id"] for e in a["evaluations"]]
    assert len(client.get("/api/v1/evaluations").json()) == 3  # not 6


def test_idempotency_key_with_a_different_payload_is_rejected(
    client: TestClient, seed_user_id: str, scorecard, aws  # noqa: F811
) -> None:
    assert _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "key-x").status_code == 202
    clash = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(2), "key-x")
    assert clash.status_code == 422 and "different request" in clash.json()["detail"]


def test_idempotency_keys_are_per_user_and_optional(
    client: TestClient, db_session, seed_user_id: str, scorecard, aws  # noqa: F811
) -> None:
    other = client.post(
        "/api/v1/users", json={"email": "idem-other@example.com", "name": "Other"}, headers=H(seed_user_id)
    ).json()
    a = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "same-key")
    mine = make_scorecard(db_session, uuid.UUID(other["id"]))
    b = _post_jobs_keyed(client, other["id"], mine["id"], _items(1), "same-key")
    assert a.json()["evaluations"][0]["id"] != b.json()["evaluations"][0]["id"]
    # No header: every call creates new evaluations, exactly as before.
    # one running job per user: cancel the keyed one first, then un-keyed requests create new evaluations
    client.post(f"/api/v1/evaluations/{a.json()['evaluations'][0]['id']}/cancel", headers=H(seed_user_id))
    c = post_jobs(client, seed_user_id, scorecard["id"], _items(1))
    client.post(f"/api/v1/evaluations/{c.json()['evaluations'][0]['id']}/cancel", headers=H(seed_user_id))
    d = post_jobs(client, seed_user_id, scorecard["id"], _items(1))
    assert c.json()["evaluations"][0]["id"] != d.json()["evaluations"][0]["id"]


def test_failed_request_does_not_burn_the_key(client: TestClient, seed_user_id: str, scorecard, aws) -> None:  # noqa: F811
    bad = [{"sources": [{"kind": "drive", "drive_url": "https://example.com/nope"}]}]
    assert _post_jobs_keyed(client, seed_user_id, scorecard["id"], bad, "retry-me").status_code == 422
    again = _post_jobs_keyed(client, seed_user_id, scorecard["id"], bad, "retry-me")
    assert again.status_code == 422  # validated again, not "already used"
    fixed = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "retry-me")
    assert fixed.status_code == 202  # the rejected attempts left no key behind, so a corrected retry is accepted


def test_manual_evaluation_post_is_idempotent(client: TestClient, seed_user_id: str, scorecard) -> None:  # noqa: F811
    body = {"scorecard_version_id": scorecard["version_id"], "name": "Manual", "evaluated_by": seed_user_id}
    headers = {**H(seed_user_id), "Idempotency-Key": "manual-1"}
    first = client.post("/api/v1/evaluations", json=body, headers=headers)
    second = client.post("/api/v1/evaluations", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/v1/evaluations").json()) == 1


def test_oversized_idempotency_key_is_422(client: TestClient, seed_user_id: str, scorecard, aws) -> None:  # noqa: F811
    r = _post_jobs_keyed(client, seed_user_id, scorecard["id"], _items(1), "k" * 201)
    assert r.status_code == 422


# --- /health and /ready ---------------------------------------------------------------------------------------


def test_health_is_static_and_ready_pings_the_database(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready" and r.json()["database"] == "up"


def test_ready_is_503_when_the_database_is_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    class Broken:
        async def __aenter__(self):
            raise OSError("connection refused")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(main_mod, "AsyncSessionLocal", Broken)
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["status"] == "not ready"
    assert client.get("/health").status_code == 200  # liveness is independent of the database


# --- roles ----------------------------------------------------------------------------------------------------


def test_api_role_only_enqueues_chat_turns_and_never_claims_them(
    client: TestClient, seed_user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import chat as chat_mod
    from app.db import SyncSessionLocal
    from app.deps import get_bedrock_client, get_jev_client, get_web_search_client
    from app.main import app
    from app.models.chat_session import ChatSession
    from tests.fakes import FakeBedrockClient, FakeJevClient, text_result

    monkeypatch.setattr(get_settings(), "role", "api")
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(script=[text_result("t")])
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()
    try:
        sid = str(uuid.uuid4())
        r = client.post(
            "/api/v1/chat/sessions?wait=false", json={"message": "Build X", "session_id": sid},
            headers=H(seed_user_id),
        )
        assert r.status_code == 202, r.text
        assert not chat_mod._turn_tasks  # no turn was started in the API process
        with SyncSessionLocal() as db:
            row = db.get(ChatSession, uuid.UUID(sid))
            assert row.turn_message == "Build X" and row.turn_first is True
            assert row.turn_lease_owner is None and row.pending_turn_started_at is not None  # waits for a worker
        assert client.get(f"/api/v1/chat/sessions/{sid}").json()["turn_in_progress"] is True
    finally:
        for dep in (get_bedrock_client, get_web_search_client, get_jev_client):
            app.dependency_overrides.pop(dep, None)


# --- JSON logs with ids ---------------------------------------------------------------------------------------


def test_json_log_lines_carry_evaluation_and_session_ids() -> None:
    formatter = JsonFormatter("worker")
    from app.logging_config import ContextFilter

    record = logging.LogRecord("app.x", logging.INFO, __file__, 1, "scored %s", ("KPI",), None)
    flt = ContextFilter()
    eid, sid = uuid.uuid4(), uuid.uuid4()
    with log_context(evaluation_id=eid, session_id=sid):
        flt.filter(record)
        line = json.loads(formatter.format(record))
    assert line["msg"] == "scored KPI" and line["level"] == "INFO" and line["role"] == "worker"
    assert line["evaluation_id"] == str(eid) and line["session_id"] == str(sid)
    outside = logging.LogRecord("app.x", logging.INFO, __file__, 1, "plain", (), None)
    flt.filter(outside)
    assert "evaluation_id" not in json.loads(formatter.format(outside))


async def test_context_ids_are_per_task() -> None:
    seen: dict[str, str | None] = {}
    flt = __import__("app.logging_config", fromlist=["ContextFilter"]).ContextFilter()

    async def job(name: str) -> None:
        bind_log_context(evaluation_id=name)
        await asyncio.sleep(0.01)
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
        flt.filter(rec)
        seen[name] = rec.evaluation_id

    await asyncio.gather(job("a"), job("b"))
    assert seen == {"a": "a", "b": "b"}


# --- the worker's health server ---------------------------------------------------------------------------------


async def test_worker_health_ready_and_metrics_endpoints(async_db_session) -> None:
    from app.worker import WorkerState, _handle

    state = WorkerState()
    server = await asyncio.start_server(lambda r, w: _handle(r, w, state), host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]

    async def get(path: str) -> tuple[int, dict]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(body)

    try:
        code, body = await get("/health")
        assert code == 200 and body["status"] == "ok" and body["role"] == "worker"
        code, body = await get("/ready")
        assert code == 200 and body["status"] == "ready"
        code, body = await get("/metrics")
        assert code == 200 and body["workers"] >= 1 and body["backlog_per_worker"] == 0
        state.draining = True  # during a drain the worker reports itself not ready (and /health says so)
        assert (await get("/ready"))[0] == 503
        assert (await get("/health"))[1]["status"] == "draining"
        assert (await get("/nope"))[0] == 404
    finally:
        server.close()
        await server.wait_closed()


async def test_backlog_per_worker_counts_queued_active_and_waiting_turns(async_db_session) -> None:
    from datetime import UTC, datetime

    from app import metrics
    from app.models.chat_session import ChatSession
    from app.models.enums import EvaluationStatus
    from tests.ai_eval_helpers import seed_queued, seed_scorecard

    owner, _sc, version, _ = await seed_scorecard(async_db_session)
    await seed_queued(async_db_session, owner, version, 3)  # queued
    await seed_queued(async_db_session, owner, version, 1, status=EvaluationStatus.SCORING)  # active
    async_db_session.add(
        ChatSession(user_id=owner.id, pending_turn_started_at=datetime.now(UTC), turn_message="hi", turn_first=True)
    )
    await async_db_session.commit()
    snap = await metrics.backlog_snapshot()
    assert (snap["queued_evaluations"], snap["active_evaluations"], snap["waiting_chat_turns"]) == (3, 1, 1)
    assert snap["workers"] == 1 and snap["backlog_per_worker"] == 5.0
