"""AI provider selection (app/ai/providers.py): availability rules, the effective-provider rules, the stored-choice
cache, model resolution per client, client singletons, Jev-key isolation and the ignored legacy settings."""

from __future__ import annotations

import logging
import os

import pytest
from sqlalchemy.orm import Session

from app.ai import providers
from app.ai.bedrock_client import BedrockClient
from app.ai.jev_client import JevClient
from app.ai.openrouter_client import OpenRouterClient
from app.config import Settings, get_settings
from app.models.app_setting import AppSetting
from tests.fakes import FakeBedrockClient, FakeOpenRouterClient

BEDROCK_IDS = providers.ModelSet(
    chat="zai.glm-5",
    judge="zai.glm-4.7-flash",
    master="us.meta.llama4-maverick-17b-instruct-v1:0",
    embedding="amazon.titan-embed-text-v2:0",
)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# --- availability -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, False),
        ({"AWS_BEARER_TOKEN_BEDROCK": "A"}, False),  # compose's "A" + empty secret
        ({"AWS_BEARER_TOKEN_BEDROCK": " A "}, False),
        ({"AWS_BEARER_TOKEN_BEDROCK": "Abedrock-key"}, True),
        ({"AWS_ACCESS_KEY_ID": "AKID"}, False),
        ({"AWS_SECRET_ACCESS_KEY": "secret"}, False),
        ({"AWS_ACCESS_KEY_ID": "AKID", "AWS_SECRET_ACCESS_KEY": "secret"}, True),
        ({"AWS_PROFILE": "dev"}, True),
        ({"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/x"}, True),
        ({"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials"}, True),
        ({"AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token"}, True),
        ({"AWS_PROFILE": "  "}, False),
    ],
)
def test_bedrock_availability_from_the_environment(environ, expected) -> None:
    assert providers.availability(_settings(), environ)["bedrock"] is expected


def test_bedrock_available_setting_overrides_detection() -> None:
    creds = {"AWS_BEARER_TOKEN_BEDROCK": "Akey"}
    assert providers.availability(_settings(bedrock_available=False), creds)["bedrock"] is False
    assert providers.availability(_settings(bedrock_available=True), {})["bedrock"] is True
    assert providers.unavailable_reason("bedrock", _settings(bedrock_available=False), creds) == (
        "BEDROCK_AVAILABLE is false on the server"
    )
    assert providers.unavailable_reason("bedrock", _settings(), {}) == "no AWS Bedrock credentials on the server"


def test_bedrock_available_env_values(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_AVAILABLE", "")  # what compose passes when unset
    assert _settings().bedrock_available is None
    monkeypatch.setenv("BEDROCK_AVAILABLE", "false")
    assert _settings().bedrock_available is False
    monkeypatch.setenv("BEDROCK_AVAILABLE", "true")
    assert _settings().bedrock_available is True


@pytest.mark.parametrize(("key", "expected"), [("", False), ("   ", False), ("k", True)])
def test_openrouter_availability_needs_a_non_blank_key(key, expected) -> None:
    assert providers.availability(_settings(openrouter_api_key=key), {})["openrouter"] is expected


def test_jev_key_and_openrouter_key_are_independent(monkeypatch) -> None:
    only_jev = _settings(openrouter_jev_api="jev-key", openrouter_api_key="")
    assert providers.availability(only_jev, {})["openrouter"] is False
    assert providers.unavailable_reason("openrouter", only_jev, {}) == "OPENROUTER_API_KEY is not set"
    # The Jev gate never borrows the provider key.
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_api_key", "provider-key")
    monkeypatch.setattr(s, "openrouter_jev_api", "")
    assert not JevClient().is_configured


def test_legacy_env_names_do_nothing_but_are_reported(monkeypatch, caplog) -> None:
    legacy = {
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_BASE_URL": "https://legacy.example.com/v1",
        "OPENROUTER_CHAT_MODEL_ID": "openai/gpt-6-luna",
        "OPENROUTER_JUDGE_MODEL_ID": "x/judge",
        "OPENROUTER_MASTER_MODEL_ID": "x/master",
        "OPENROUTER_EMBEDDING_MODEL_ID": "x/embed",
        "OPENROUTER_SEARCH_MODEL_ID": "x/search",
        "OPENROUTER_REASONING_EFFORT": "high",
        "OPENROUTER_WEB_SEARCH_ENGINE": "exa",
    }
    for name, value in legacy.items():
        monkeypatch.setenv(name, value)
    s = _settings()
    assert s.openrouter_agent_model == "openai/gpt-5-mini"
    assert s.openrouter_embed_model == "openai/text-embedding-3-small"
    assert s.openrouter_api_base == "https://openrouter.ai/api/v1"
    assert s.openrouter_reasoning == "low" and s.openrouter_search_engine == ""
    assert providers.availability(s, {})["openrouter"] is False  # LLM_PROVIDER selects nothing
    monkeypatch.setattr(providers.logger, "disabled", False)  # alembic's fileConfig() disables existing loggers
    with caplog.at_level(logging.WARNING, logger="app.ai.providers"):
        found = providers.warn_legacy_env()
    assert set(found) == set(legacy)
    assert "LLM_PROVIDER" in caplog.text and "gpt-6-luna" not in caplog.text and "legacy.example.com" not in caplog.text
    assert providers.warn_legacy_env({}) == []


# --- effective provider ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bedrock", "openrouter", "stored", "expected"),
    [
        (True, True, None, "bedrock"),
        (True, True, "openrouter", "openrouter"),
        (True, True, "bedrock", "bedrock"),
        (True, False, "openrouter", "bedrock"),  # saved choice lost its key: fall back
        (False, True, None, "openrouter"),
        (False, True, "bedrock", "openrouter"),
        (False, False, "openrouter", None),
        (False, False, None, None),
        (True, True, "azure", "bedrock"),
    ],
)
def test_effective_provider_rules(monkeypatch, bedrock, openrouter, stored, expected) -> None:
    monkeypatch.setattr(providers, "availability", lambda *a, **k: {"bedrock": bedrock, "openrouter": openrouter})
    assert providers.effective_provider(stored) == expected


async def test_active_provider_never_raises_and_defaults_to_bedrock(monkeypatch) -> None:
    async def broken():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(providers, "stored_provider", broken)
    assert await providers.active_provider() == "bedrock"

    async def stored():
        return "openrouter"

    monkeypatch.setattr(providers, "stored_provider", stored)
    monkeypatch.setattr(providers, "availability", lambda *a, **k: {"bedrock": False, "openrouter": False})
    assert await providers.active_provider() == "bedrock"  # nothing available: Bedrock's usual errors follow
    monkeypatch.setattr(providers, "availability", lambda *a, **k: {"bedrock": True, "openrouter": True})
    assert await providers.active_provider() == "openrouter"
    assert isinstance(await providers.active_client(), OpenRouterClient)


async def test_provider_for_job_keeps_the_jobs_provider_while_available(monkeypatch) -> None:
    async def stored():
        return "bedrock"

    monkeypatch.setattr(providers, "stored_provider", stored)
    monkeypatch.setattr(providers, "unavailable_reason", lambda p, *a, **k: None)
    monkeypatch.setattr(providers, "availability", lambda *a, **k: {"bedrock": True, "openrouter": True})
    assert await providers.provider_for_job("openrouter") == "openrouter"
    assert await providers.provider_for_job(None) == "bedrock"
    assert await providers.provider_for_job("nonsense") == "bedrock"
    monkeypatch.setattr(
        providers, "unavailable_reason", lambda p, *a, **k: "gone" if p == "openrouter" else None
    )
    assert await providers.provider_for_job("openrouter") == "bedrock"


# --- models / clients -----------------------------------------------------------------------------------------


def test_models_for_each_provider() -> None:
    assert providers.models_for("bedrock") == BEDROCK_IDS
    gpt = "openai/gpt-5-mini"
    assert providers.models_for("openrouter") == providers.ModelSet(gpt, gpt, gpt, "openai/text-embedding-3-small")


def test_models_of_falls_back_per_field() -> None:
    assert providers.provider_of(FakeBedrockClient()) == "bedrock"
    assert providers.models_of(FakeBedrockClient()) == BEDROCK_IDS  # the fake has no model attributes
    assert providers.embedding_model_of(FakeBedrockClient()) is None
    fake_or = FakeOpenRouterClient()
    assert providers.provider_of(fake_or) == "openrouter"
    assert providers.models_of(fake_or) == providers.ModelSet(
        "openai/gpt-5-mini", "openai/gpt-5-mini", "openai/gpt-5-mini", "fake-embed"
    )
    assert providers.embedding_model_of(fake_or) == "fake-embed"

    class Partial:
        provider = "openrouter"
        judge_model_id = "x/judge"

    m = providers.models_of(Partial())
    assert m.judge == "x/judge" and m.chat == "openai/gpt-5-mini" and m.embedding == "openai/text-embedding-3-small"

    class Odd:
        provider = "azure"
        chat_model_id = ""

    assert providers.provider_of(Odd()) == "bedrock" and providers.models_of(Odd()) == BEDROCK_IDS
    b = BedrockClient()
    assert providers.models_of(b) == BEDROCK_IDS and providers.embedding_model_of(b) == BEDROCK_IDS.embedding


def test_client_singletons() -> None:
    b, o = providers.client_for("bedrock"), providers.client_for("openrouter")
    assert isinstance(b, BedrockClient) and isinstance(o, OpenRouterClient)
    assert providers.client_for("bedrock") is b and providers.client_for("openrouter") is o
    assert providers.client_for("whatever") is b
    providers.reset_clients()
    assert providers.client_for("bedrock") is not b


async def test_get_bedrock_client_dependency_returns_the_active_client(monkeypatch) -> None:
    from app.deps import get_bedrock_client

    async def stored():
        return "openrouter"

    monkeypatch.setattr(providers, "stored_provider", stored)
    monkeypatch.setattr(providers, "availability", lambda *a, **k: {"bedrock": True, "openrouter": True})
    assert await get_bedrock_client() is providers.client_for("openrouter")


# --- stored choice + cache --------------------------------------------------------------------------------------


def _store(db: Session, provider: str) -> None:
    row = db.query(AppSetting).filter_by(key=providers.SETTING_KEY).one_or_none()
    if row is None:
        db.add(AppSetting(key=providers.SETTING_KEY, value={"provider": provider}))
    else:
        row.value = {"provider": provider}
    db.commit()


async def test_stored_provider_is_cached_for_the_ttl(db_session: Session, monkeypatch) -> None:
    assert await providers.stored_provider() is None
    providers.invalidate_cache()
    _store(db_session, "openrouter")
    assert await providers.stored_provider() == "openrouter"
    _store(db_session, "bedrock")
    assert await providers.stored_provider() == "openrouter"  # still cached
    later = providers._clock() + providers.CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(providers, "_clock", lambda: later)
    assert await providers.stored_provider() == "bedrock"  # TTL expired
    _store(db_session, "openrouter")
    providers.invalidate_cache()
    assert await providers.stored_provider() == "openrouter"


async def test_stored_provider_ignores_unknown_values(db_session: Session) -> None:
    db_session.add(AppSetting(key=providers.SETTING_KEY, value={"provider": "azure"}))
    db_session.commit()
    assert await providers.stored_provider() is None
    assert providers.parse_provider("bedrock") == "bedrock" and providers.parse_provider({"x": 1}) is None


async def test_database_errors_return_the_last_known_value(db_session: Session, monkeypatch) -> None:
    import app.db

    _store(db_session, "openrouter")
    assert await providers.stored_provider() == "openrouter"

    def broken():
        raise ConnectionError("database down")

    monkeypatch.setattr(app.db, "AsyncSessionLocal", broken)
    later = providers._clock() + providers.CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(providers, "_clock", lambda: later)
    assert await providers.stored_provider() == "openrouter"
    providers.invalidate_cache()
    assert await providers.stored_provider() is None
    assert await providers.active_provider() == "bedrock"


async def test_set_provider_rejects_unknown_and_locked(async_db_session, monkeypatch) -> None:
    with pytest.raises(ValueError):
        await providers.set_provider(async_db_session, "azure", None)
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    with pytest.raises(providers.ProviderLocked) as ei:
        await providers.set_provider(async_db_session, "openrouter", None)
    assert str(ei.value) == "OpenRouter is not available: OPENROUTER_API_KEY is not set"
    assert ei.value.provider == "openrouter"


async def test_set_provider_saves_audits_and_primes_the_cache(async_db_session, monkeypatch) -> None:
    from sqlalchemy import select

    from app.models.audit_log import AuditLog

    monkeypatch.setattr(get_settings(), "openrouter_api_key", "k")
    row, previous = await providers.set_provider(async_db_session, "openrouter", None)
    assert previous is None and row.value == {"provider": "openrouter"} and row.updated_at is not None
    assert await providers.stored_provider() == "openrouter"
    _row2, previous2 = await providers.set_provider(async_db_session, "openrouter", None)
    assert previous2 == "openrouter"  # unchanged: no write, no audit
    audits = (await async_db_session.execute(select(AuditLog).where(AuditLog.entity_type == "app_setting"))).scalars()
    assert [a.diff for a in audits] == [
        {"key": "ai_provider", "from": None, "to": "openrouter", "event": "ai_provider_changed"}
    ]


def test_shared_aws_config_counts_as_bedrock_credentials_only_for_the_real_environment(monkeypatch, tmp_path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text("[default]\naws_access_key_id = AKIDEXAMPLE\naws_secret_access_key = notasecret\n")
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                 "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                 "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    providers.shared_aws_credentials.cache_clear()
    assert providers.shared_aws_credentials() is True
    assert providers._bedrock_credentials_present(os.environ) is True
    assert providers._bedrock_credentials_present({}) is False  # an injected environment never reads ~/.aws
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    providers.shared_aws_credentials.cache_clear()
    assert providers.shared_aws_credentials() is False


async def test_stored_provider_reads_the_database_once_for_concurrent_callers(monkeypatch) -> None:
    import asyncio

    import app.db as db_module

    opened = 0
    real = db_module.AsyncSessionLocal

    def counting_session(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(db_module, "AsyncSessionLocal", counting_session)
    providers.invalidate_cache()
    results = await asyncio.gather(*(providers.stored_provider() for _ in range(20)))
    assert len(set(results)) == 1 and opened == 1  # one database read served all 20 callers
