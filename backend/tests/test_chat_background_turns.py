"""Background chat turns (app/api/v1/chat.py "Background turns"): POST returns 202 immediately and
the LangGraph turn runs as a server-side task; clients poll `GET /chat/sessions/{id}`.

The rest of the suite runs turns inline (conftest sets CHAT_TURNS_INLINE=true); every test here
opts into the real default with `?wait=false`. `TestClient` keeps its event loop alive between
requests, so the background task makes progress while the test polls. Bedrock is a
`FakeBedrockClient` (no live calls).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.ai.bedrock_client import BedrockUnavailableError
from app.config import get_settings
from app.deps import get_bedrock_client, get_jev_client, get_web_search_client
from app.main import app
from tests.conftest import make_access_token
from tests.fakes import FakeBedrockClient, FakeJevClient, full_rubric, text_result, tool_use_result

_DRAFT_PATCH = {
    "name": "Support Ticket Quality",
    "purpose": "Rate support ticket resolutions.",
    "domain": "Customer Support",
    "audience": "Support leads",
    "target_score": 8,
    "kpis": [
        {"name": "Accuracy", "weight": 60, "level": 1,
         "guidelines": full_rubric()},
        {"name": "Tone", "weight": 40, "level": 1,
         "guidelines": full_rubric()},
    ],
}


@pytest.fixture(autouse=True)
def _inert_externals():
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()
    yield
    for dep in (get_bedrock_client, get_web_search_client, get_jev_client):
        app.dependency_overrides.pop(dep, None)


def _poll(client: TestClient, session_id: str, timeout: float = 30.0) -> dict:
    """Polls GET /chat/sessions/{id} until the turn is no longer in progress (what the frontend does)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/chat/sessions/{session_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if not body["turn_in_progress"]:
            return body
        time.sleep(0.1)
    raise AssertionError("turn did not finish in time")


def _user(client: TestClient, seed_user_id: str) -> dict:
    return client.post(
        "/api/v1/users", json={"email": f"bg-{uuid.uuid4().hex[:6]}@example.com", "name": "BG"},
        headers={"X-User-Id": seed_user_id},
    ).json()


def test_post_returns_202_immediately_then_polling_yields_the_result(client: TestClient, seed_user_id: str) -> None:
    user = _user(client, seed_user_id)
    release = threading.Event()
    calls = {"n": 0}

    def converse(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return text_result("Support Ticket Quality Review")  # title call
        assert release.wait(timeout=20)  # the turn is held open until the test says so
        return tool_use_result(
            "ask_clarification", {"question": "What is the purpose?", "options": ["QA"], "missing_fields": ["purpose"]}
        )

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=converse)
    sid = str(uuid.uuid4())
    started = time.monotonic()
    r = client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "Build a scorecard.", "session_id": sid},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 202, r.text
    assert time.monotonic() - started < 5  # did not wait for the (blocked) turn
    body = r.json()
    assert body["session_id"] == sid and body["turn_in_progress"] is True and body["draft"] == {}

    mid = client.get(f"/api/v1/chat/sessions/{sid}").json()
    assert mid["turn_in_progress"] is True and mid["turn_error"] is None

    release.set()
    final = _poll(client, sid)
    assert final["status"] == "awaiting_clarification"
    assert final["question"]["question"] == "What is the purpose?"
    assert final["title"] == "Support Ticket Quality Review" and final["turn_error"] is None

    messages = client.get(f"/api/v1/chat/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Build a scorecard."


def test_followup_message_runs_in_background_and_confirms(client: TestClient, seed_user_id: str) -> None:
    user = _user(client, seed_user_id)
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[
            text_result("Support Quality"),
            tool_use_result(
                "ask_clarification", {"question": "Purpose?", "options": [], "missing_fields": ["purpose"]}
            ),
        ]
    )
    sid = str(uuid.uuid4())
    assert client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "hi", "session_id": sid},
        headers={"X-User-Id": user["id"]},
    ).status_code == 202
    assert _poll(client, sid)["status"] == "awaiting_clarification"

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft", {"patch": _DRAFT_PATCH, "confirmed": True, "assistant_message": "Saved."}
            )
        ]
    )
    r = client.post(
        f"/api/v1/chat/sessions/{sid}/messages?wait=false", json={"message": "Rate ticket resolutions."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 202 and r.json()["turn_in_progress"] is True
    final = _poll(client, sid)
    assert final["status"] == "confirmed" and final["materialized_scorecard_id"]
    assert final["assistant_message"] == "Saved."
    assert [m["role"] for m in client.get(f"/api/v1/chat/sessions/{sid}/messages").json()] == [
        "user", "assistant", "user", "assistant",
    ]


def test_failed_background_turn_records_the_error_and_clears_the_marker(client: TestClient, seed_user_id: str) -> None:
    user = _user(client, seed_user_id)

    def down(**_kw):
        raise BedrockUnavailableError("simulated outage")

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=down)
    sid = str(uuid.uuid4())
    assert client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "hi", "session_id": sid},
        headers={"X-User-Id": user["id"]},
    ).status_code == 202
    final = _poll(client, sid)
    assert final["turn_in_progress"] is False
    assert final["turn_error_code"] == "bedrock_unavailable" and "simulated outage" in final["turn_error"]

    # The error is cleared when the next turn starts, and a retry on the same session works.
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[tool_use_result("ask_clarification", {"question": "Purpose?", "options": [], "missing_fields": []})]
    )
    r = client.post(
        f"/api/v1/chat/sessions/{sid}/messages?wait=false", json={"message": "hi"}, headers={"X-User-Id": user["id"]}
    )
    assert r.status_code == 202
    ok = _poll(client, sid)
    assert ok["turn_error"] is None and ok["status"] == "awaiting_clarification"


def test_second_message_while_a_turn_runs_is_rejected_with_409(client: TestClient, seed_user_id: str) -> None:
    user = _user(client, seed_user_id)
    release = threading.Event()

    def slow(**_kw):
        release.wait(timeout=20)
        return tool_use_result("ask_clarification", {"question": "Q?", "options": [], "missing_fields": []})

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=slow)
    sid = str(uuid.uuid4())
    client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "hi", "session_id": sid}, headers={"X-User-Id": user["id"]}
    )
    r = client.post(
        f"/api/v1/chat/sessions/{sid}/messages?wait=false", json={"message": "again"}, headers={"X-User-Id": user["id"]}
    )
    assert r.status_code == 409
    release.set()
    _poll(client, sid)


def test_turn_exceeding_the_time_limit_is_stopped_and_reported(
    client: TestClient, seed_user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(client, seed_user_id)
    monkeypatch.setattr(get_settings(), "chat_turn_timeout_seconds", 1)

    def slow(**_kw):
        time.sleep(3)
        return text_result("late")

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=slow)
    sid = str(uuid.uuid4())
    client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "hi", "session_id": sid}, headers={"X-User-Id": user["id"]}
    )
    final = _poll(client, sid, timeout=30)
    assert final["turn_error_code"] == "timeout" and final["turn_in_progress"] is False


def test_startup_recovers_turns_whose_lease_expired_but_leaves_live_ones(db_session, seed_user_id: str) -> None:
    """A turn whose worker died (lease expired) is recorded as interrupted at the next worker start; a turn whose
    owner is alive (unexpired lease) must NOT be touched - the old "clear every marker at boot" recovery could
    not tell the two apart once more than one process existed."""
    from datetime import UTC, datetime, timedelta

    from app.models.chat_session import ChatSession

    now = datetime.now(UTC)
    dead = ChatSession(
        user_id=uuid.UUID(seed_user_id), pending_turn_started_at=now, turn_message="hi", turn_first=True,
        turn_lease_owner="dead-worker", turn_lease_expires_at=now - timedelta(seconds=5),
    )
    alive = ChatSession(
        user_id=uuid.UUID(seed_user_id), pending_turn_started_at=now, turn_message="hi", turn_first=True,
        turn_lease_owner="live-worker", turn_lease_expires_at=now + timedelta(seconds=300),
    )
    db_session.add_all([dead, alive])
    db_session.commit()
    assert dead.turn_in_progress is True and alive.turn_in_progress is True

    with TestClient(app) as fresh:  # entering the app runs the lifespan startup (reaps expired leases)
        auth = {"Authorization": f"Bearer {make_access_token(seed_user_id)}"}
        dead_body = fresh.get(f"/api/v1/chat/sessions/{dead.id}", headers=auth).json()
        alive_body = fresh.get(f"/api/v1/chat/sessions/{alive.id}", headers=auth).json()
    assert dead_body["turn_in_progress"] is False
    assert dead_body["turn_error_code"] == "interrupted" and "restarted" in dead_body["turn_error"]
    assert alive_body["turn_in_progress"] is True and alive_body["turn_error"] is None


def test_shutdown_cancels_running_turns_and_records_interruption(db_session, seed_user_id: str) -> None:
    from app.models.chat_session import ChatSession

    sid = str(uuid.uuid4())
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        converse_fn=lambda **_kw: (time.sleep(2), text_result("x"))[1]
    )
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/chat/sessions?wait=false", json={"message": "hi", "session_id": sid},
            headers={"Authorization": f"Bearer {make_access_token(seed_user_id)}"},
        )
        assert r.status_code == 202
        time.sleep(0.3)  # the task is now inside the (sleeping) model call
    # leaving the context ran shutdown_background_turns()
    db_session.expire_all()
    row = db_session.get(ChatSession, uuid.UUID(sid))
    assert row is not None and row.pending_turn_started_at is None
    assert row.last_turn_error_code == "interrupted"


def test_first_message_is_persisted_before_the_turn_finishes_and_never_duplicated(
    client: TestClient, seed_user_id: str
) -> None:
    user = _user(client, seed_user_id)
    release = threading.Event()
    calls = {"n": 0}

    def converse(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return text_result("A Title")
        assert release.wait(timeout=20)
        return tool_use_result("ask_clarification", {"question": "Purpose?", "options": [], "missing_fields": []})

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=converse)
    sid = str(uuid.uuid4())
    assert client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "Build a scorecard.", "session_id": sid},
        headers={"X-User-Id": user["id"]},
    ).status_code == 202
    mid = client.get(f"/api/v1/chat/sessions/{sid}/messages").json()  # immediately, turn still running
    assert [(m["role"], m["content"]) for m in mid] == [("user", "Build a scorecard.")]
    assert client.get(f"/api/v1/chat/sessions/{sid}").json()["turn_in_progress"] is True
    release.set()
    _poll(client, sid)
    done = client.get(f"/api/v1/chat/sessions/{sid}/messages").json()
    assert [m["role"] for m in done] == ["user", "assistant"]  # no duplicate user message


def test_failed_first_turn_keeps_session_user_message_and_error_and_retry_does_not_duplicate(
    client: TestClient, seed_user_id: str
) -> None:
    user = _user(client, seed_user_id)

    def down(**_kw):
        raise BedrockUnavailableError("outage")

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=down)
    sid = str(uuid.uuid4())
    client.post(
        "/api/v1/chat/sessions?wait=false", json={"message": "Build X", "session_id": sid},
        headers={"X-User-Id": user["id"]},
    )
    final = _poll(client, sid)
    assert final["turn_error_code"] == "bedrock_unavailable"
    msgs = client.get(f"/api/v1/chat/sessions/{sid}/messages").json()
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "Build X")]

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(
        script=[tool_use_result("ask_clarification", {"question": "Purpose?", "options": [], "missing_fields": []})]
    )
    assert client.post(
        f"/api/v1/chat/sessions/{sid}/messages?wait=false", json={"message": "Build X"},
        headers={"X-User-Id": user["id"]},
    ).status_code == 202
    _poll(client, sid)
    msgs = client.get(f"/api/v1/chat/sessions/{sid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # retry with the same text: still one user message
