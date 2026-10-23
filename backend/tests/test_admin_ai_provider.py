"""Admin AI-provider setting (`GET/PUT /api/v1/admin/settings/ai-provider`): response shape, admin-only access,
locked providers (409), validation (422), the audit row, the no-op PUT and the embedding backfill hook."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import providers
from app.api.v1 import admin_settings
from app.config import get_settings
from app.models.app_setting import AppSetting
from app.models.audit_log import AuditLog
from app.models.enums import AuditAction
from tests.sharing_world import Team, hdr, team  # noqa: F401 - fixture

URL = "/api/v1/admin/settings/ai-provider"
GPT = "openai/gpt-5-mini"


@pytest.fixture()
def backfills(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(admin_settings, "_schedule_backfill", calls.append)
    return calls


@pytest.fixture()
def keys(monkeypatch):
    """Both providers available unless a test says otherwise; AgentCore web search not configured."""
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_api_key", "k")
    monkeypatch.setattr(s, "bedrock_available", True)
    monkeypatch.setattr(s, "agentcore_gateway_web_search_url", "")
    providers.reset_clients()
    return s


def _audits(db: Session) -> list[AuditLog]:
    db.expire_all()
    stmt = select(AuditLog).where(AuditLog.entity_type == "app_setting").order_by(AuditLog.created_at)
    return list(db.execute(stmt).scalars())


def test_get_shape_when_nothing_is_saved(client: TestClient, team: Team, keys) -> None:  # noqa: F811
    r = client.get(URL, headers=hdr(team.admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "provider", "effective_provider", "available", "models", "web_search", "updated_at", "updated_by",
    }
    assert body["provider"] is None and body["effective_provider"] == "bedrock"
    assert body["available"] == {"bedrock": True, "openrouter": True}
    assert body["models"]["bedrock"] == {
        "chat": "zai.glm-5",
        "judge": "zai.glm-4.7-flash",
        "master": "us.meta.llama4-maverick-17b-instruct-v1:0",
        "embedding": "amazon.titan-embed-text-v2:0",
        "vision": ["moonshotai.kimi-k2.5", "qwen.qwen3-vl-235b-a22b", "us.meta.llama4-maverick-17b-instruct-v1:0"],
        "transcription": ["amazon.transcribe", "mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"],
    }
    assert body["models"]["openrouter"] == {
        "chat": GPT, "judge": GPT, "master": GPT, "embedding": "openai/text-embedding-3-small",
        "vision": ["qwen/qwen3.7-flash", "qwen/qwen3-vl-235b-a22b-instruct"],
        "transcription": ["mistralai/voxtral-small-24b-2507", "mistralai/voxtral-mini-3b-2507"],
    }
    assert body["web_search"] == {"agentcore": False, "effective": "none"}
    assert body["updated_at"] is None and body["updated_by"] is None


def test_get_with_no_credentials_at_all(client: TestClient, team: Team, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "bedrock_available", False)
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    body = client.get(URL, headers=hdr(team.admin)).json()
    assert body["available"] == {"bedrock": False, "openrouter": False}
    assert body["effective_provider"] is None and body["provider"] is None


def test_switch_audits_schedules_backfill_and_takes_effect(
    client: TestClient, team: Team, keys, backfills, db_session: Session  # noqa: F811
) -> None:
    r = client.put(URL, json={"provider": "openrouter"}, headers=hdr(team.admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "openrouter" and body["effective_provider"] == "openrouter"
    assert body["updated_by"] == {"id": str(team.admin.id), "name": "Ada Admin"} and body["updated_at"]
    assert body["web_search"] == {"agentcore": False, "effective": "openrouter"}
    assert backfills == ["openrouter"]
    audits = _audits(db_session)
    assert len(audits) == 1
    assert audits[0].action == AuditAction.UPDATE and audits[0].actor_id == team.admin.id
    assert audits[0].diff == {"key": "ai_provider", "from": None, "to": "openrouter", "event": "ai_provider_changed"}
    row = db_session.execute(select(AppSetting)).scalar_one()
    assert row.key == "ai_provider" and row.value == {"provider": "openrouter"} and row.updated_by == team.admin.id
    assert audits[0].entity_id == row.id
    # The GET agrees, and so does the provider new work resolves to.
    assert client.get(URL, headers=hdr(team.admin)).json()["provider"] == "openrouter"

    # Same value again: a no-op (no audit row, no backfill).
    again = client.put(URL, json={"provider": "openrouter"}, headers=hdr(team.admin))
    assert again.status_code == 200 and again.json()["provider"] == "openrouter"
    assert len(_audits(db_session)) == 1 and backfills == ["openrouter"]

    back = client.put(URL, json={"provider": "bedrock"}, headers=hdr(team.admin))
    assert back.status_code == 200 and back.json()["effective_provider"] == "bedrock"
    assert backfills == ["openrouter", "bedrock"]
    assert [(a.diff["from"], a.diff["to"]) for a in _audits(db_session)] == [
        (None, "openrouter"),
        ("openrouter", "bedrock"),
    ]


async def test_active_provider_follows_the_saved_choice(
    client: TestClient, team: Team, keys, backfills  # noqa: F811
) -> None:
    assert client.put(URL, json={"provider": "openrouter"}, headers=hdr(team.admin)).status_code == 200
    assert await providers.active_provider() == "openrouter"
    assert providers.provider_of(await providers.active_client()) == "openrouter"


def test_locked_provider_is_409_and_writes_nothing(
    client: TestClient, team: Team, keys, backfills, monkeypatch, db_session: Session  # noqa: F811
) -> None:
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    r = client.put(URL, json={"provider": "openrouter"}, headers=hdr(team.admin))
    assert r.status_code == 409
    assert r.json() == {"detail": "OpenRouter is not available: OPENROUTER_API_KEY is not set"}
    monkeypatch.setattr(get_settings(), "bedrock_available", False)
    r = client.put(URL, json={"provider": "bedrock"}, headers=hdr(team.admin))
    assert r.status_code == 409 and r.json()["detail"].startswith("Bedrock is not available: ")
    assert backfills == [] and _audits(db_session) == []
    assert db_session.execute(select(AppSetting)).first() is None


def test_saved_choice_that_lost_its_key_falls_back(
    client: TestClient, team: Team, keys, backfills, monkeypatch  # noqa: F811
) -> None:
    assert client.put(URL, json={"provider": "openrouter"}, headers=hdr(team.admin)).status_code == 200
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    body = client.get(URL, headers=hdr(team.admin)).json()
    assert body["provider"] == "openrouter" and body["effective_provider"] == "bedrock"
    assert body["available"] == {"bedrock": True, "openrouter": False}
    assert body["web_search"]["effective"] == "none"


@pytest.mark.parametrize(
    "payload", [{"provider": "azure"}, {}, {"provider": None}, {"provider": "bedrock", "extra": 1}, {"provider": 1}]
)
def test_bad_bodies_are_422(client: TestClient, team: Team, keys, backfills, payload) -> None:  # noqa: F811
    assert client.put(URL, json=payload, headers=hdr(team.admin)).status_code == 422
    assert backfills == []


def test_admin_only(client: TestClient, team: Team, keys, backfills) -> None:  # noqa: F811
    for method, body in (("GET", None), ("PUT", {"provider": "openrouter"})):
        assert client.request(method, URL, json=body, headers=hdr(team.owner)).status_code == 403
        assert client.request(method, URL, json=body, headers={"X-Test-Anonymous": "1"}).status_code == 401
    assert backfills == []


def test_schedule_backfill_hook_calls_the_backfill_and_never_raises(monkeypatch, keys) -> None:
    seen = []
    stub = types.ModuleType("app.ai.embedding_backfill")
    stub.schedule_backfill = lambda client, *, reason: seen.append((client, reason)) or True
    monkeypatch.setitem(sys.modules, "app.ai.embedding_backfill", stub)
    admin_settings._schedule_backfill("openrouter")
    assert seen == [(providers.client_for("openrouter"), "provider_switch")]

    def boom(client, *, reason):
        raise RuntimeError("backfill exploded")

    stub.schedule_backfill = boom
    admin_settings._schedule_backfill("bedrock")  # logged, not raised
    monkeypatch.setitem(sys.modules, "app.ai.embedding_backfill", None)  # import fails
    admin_settings._schedule_backfill("bedrock")
