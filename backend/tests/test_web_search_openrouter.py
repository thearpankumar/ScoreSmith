"""OpenRouter web search (the `openrouter:web_search` server tool) and the AgentCore / OpenRouter selection in
app/ai/web_search.py. No network: httpx.MockTransport only."""

from __future__ import annotations

import contextlib
import json

import httpx
import pytest

from app.ai import web_search as ws
from app.ai.web_search import (
    AgentCoreWebSearchClient,
    OpenRouterWebSearchClient,
    extract_openrouter_results,
    web_search_backend,
    web_search_for,
)
from app.config import get_settings
from app.deps import get_web_search_client
from tests.fakes import FakeBedrockClient, FakeOpenRouterClient

KEY = "sk-or-v1-test123"


def _annotated(n: int, *, dupes: bool = False) -> dict:
    anns = [
        {
            "type": "url_citation",
            "url_citation": {"url": f"https://example.com/{i}", "title": "T" * 300, "content": "C" * 900},
        }
        for i in range(n)
    ]
    if dupes:
        anns.append(dict(anns[0]))
    anns.append({"type": "other"})
    message = {"content": "s", "annotations": anns}
    return {"provider": "OpenAI", "usage": {"cost": 0.012}, "choices": [{"message": message}]}


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.urls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        self.urls.append(str(request.url))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _client(rec, **kw) -> OpenRouterWebSearchClient:
    return OpenRouterWebSearchClient(api_key=KEY, transport=httpx.MockTransport(rec), retry_delay=0, **kw)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_agent_model", "openai/gpt-5-mini")
    monkeypatch.setattr(s, "openrouter_search_engine", "")
    monkeypatch.setattr(s, "openrouter_api_base", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(s, "openrouter_api_key", "")
    for name in (
        "agentcore_gateway_web_search_url",
        "agentcore_gateway_web_search_tool_name",
        "agentcore_gateway_aws_access_key_id",
        "agentcore_gateway_aws_secret_access_key",
    ):
        monkeypatch.setattr(s, name, "")
    ws.reset_web_search_clients()
    yield
    ws.reset_web_search_clients()


def _configure_agentcore(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "agentcore_gateway_web_search_url", "https://gateway.example.com/mcp")
    monkeypatch.setattr(s, "agentcore_gateway_web_search_tool_name", "search")
    monkeypatch.setattr(s, "agentcore_gateway_aws_access_key_id", "AKID")
    monkeypatch.setattr(s, "agentcore_gateway_aws_secret_access_key", "secret")
    ws.reset_web_search_clients()


# --- parsing --------------------------------------------------------------------------------------------------


def test_extract_maps_url_citations_dedupes_truncates_and_caps() -> None:
    results = extract_openrouter_results(_annotated(15))
    assert len(results) == 10
    assert results[0].url == "https://example.com/0"
    assert len(results[0].title) == 200 and len(results[0].snippet) == 500
    assert [r.url for r in extract_openrouter_results(_annotated(2, dupes=True))] == [
        "https://example.com/0",
        "https://example.com/1",
    ]
    assert len(extract_openrouter_results(_annotated(8), max_results=3)) == 3


@pytest.mark.parametrize(
    "body",
    [{}, {"choices": []}, {"choices": [{"message": {"annotations": "x"}}]}, {"choices": "nope"}, [], None,
     {"choices": [{"message": {"annotations": [{"type": "url_citation", "url_citation": {"title": "no url"}}]}}]}],
)
def test_extract_never_raises_on_malformed_bodies(body) -> None:
    assert extract_openrouter_results(body) == []


# --- request ------------------------------------------------------------------------------------------------


async def test_search_request_uses_the_server_tool(monkeypatch) -> None:
    slots = []
    real = ws.get_semaphore

    def recording(name, **kw):
        slots.append(name)
        return real(name, **kw)

    monkeypatch.setattr(ws, "get_semaphore", recording)
    rec = Recorder(httpx.Response(200, json=_annotated(3)))
    results = await _client(rec, max_results=5).search("support KPIs")
    assert [r.url for r in results] == [f"https://example.com/{i}" for i in range(3)]
    body = rec.bodies[0]
    assert rec.urls[0] == "https://openrouter.ai/api/v1/chat/completions"
    assert body["model"] == "openai/gpt-5-mini"
    assert body["tools"] == [
        {"type": "openrouter:web_search", "parameters": {"max_results": 5, "search_context_size": "low"}}
    ]
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert body["max_tokens"] == 3000
    assert "plugins" not in body and "parallel_tool_calls" not in body
    assert "support KPIs" in body["messages"][0]["content"]
    assert rec.headers[0]["authorization"] == f"Bearer {KEY}"
    assert slots == ["web_search"]  # the same cluster-wide slot as the AgentCore client


async def test_engine_is_sent_only_when_configured(monkeypatch) -> None:
    rec = Recorder(httpx.Response(200, json=_annotated(1)))
    await _client(rec, engine="exa").search("q")
    monkeypatch.setattr(get_settings(), "openrouter_search_engine", "native")
    await _client(rec).search("q")
    assert rec.bodies[0]["tools"][0]["parameters"]["engine"] == "exa"
    assert rec.bodies[1]["tools"][0]["parameters"]["engine"] == "native"


@pytest.mark.parametrize(
    ("responses", "calls"),
    [
        ([httpx.Response(500, text="boom")], 3),
        ([httpx.Response(429, json={"error": {"message": "slow"}})], 3),
        ([httpx.Response(401, json={"error": {"message": f"bad {KEY}"}})], 1),
        ([httpx.Response(402, json={"error": {"message": "credits"}})], 1),
        ([httpx.Response(200, json={"error": {"code": 400, "message": "bad request"}})], 1),
        ([httpx.Response(200, text="not json")], 3),
        ([httpx.Response(502, text="x"), httpx.Response(200, json=_annotated(2))], 2),
    ],
)
async def test_search_never_raises(responses, calls, caplog, monkeypatch) -> None:
    monkeypatch.setattr(ws.logger, "disabled", False)  # alembic's fileConfig() disables existing loggers
    rec = Recorder(*responses)
    results = await _client(rec).search("q")
    assert len(rec.bodies) == calls
    assert results == ([] if calls != 2 else extract_openrouter_results(_annotated(2)))
    assert KEY not in caplog.text
    if calls != 2:
        assert "web_search query='q' failed" in caplog.text


async def test_transport_errors_and_timeouts_return_empty() -> None:
    async def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = OpenRouterWebSearchClient(api_key=KEY, transport=httpx.MockTransport(boom), retry_delay=0)
    assert await client.search("q") == []


async def test_unconfigured_client_returns_empty_without_network(monkeypatch) -> None:
    async def handler(request):
        raise AssertionError("no request expected")

    client = OpenRouterWebSearchClient(transport=httpx.MockTransport(handler))
    assert not client.is_configured and await client.search("q") == []
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "k")
    assert client.is_configured  # the key is read at call time


# --- selection ----------------------------------------------------------------------------------------------


def test_agentcore_wins_whenever_configured(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "k")
    _configure_agentcore(monkeypatch)
    for provider in ("bedrock", "openrouter"):
        chosen = web_search_for(provider)
        assert isinstance(chosen, AgentCoreWebSearchClient) and chosen.is_configured
        assert web_search_backend(provider) == "agentcore"


def test_openrouter_search_only_for_the_openrouter_provider_with_a_key(monkeypatch) -> None:
    assert isinstance(web_search_for("openrouter"), AgentCoreWebSearchClient)  # no key: unconfigured AgentCore
    assert web_search_backend("openrouter") == "none"
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "k")
    monkeypatch.setattr(get_settings(), "openrouter_search_max_results", 4)
    ws.reset_web_search_clients()
    chosen = web_search_for("openrouter")
    assert isinstance(chosen, OpenRouterWebSearchClient) and chosen._max_results == 4
    assert web_search_for("openrouter") is chosen  # a process singleton
    assert web_search_backend("openrouter") == "openrouter"
    bedrock = web_search_for("bedrock")
    assert isinstance(bedrock, AgentCoreWebSearchClient) and not bedrock.is_configured
    assert web_search_backend("bedrock") == "none" and web_search_backend(None) == "none"


async def test_dependency_follows_the_requests_llm(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "k")
    assert isinstance(await get_web_search_client(FakeOpenRouterClient()), OpenRouterWebSearchClient)
    assert isinstance(await get_web_search_client(FakeBedrockClient()), AgentCoreWebSearchClient)


async def test_openrouter_search_respects_its_own_concurrency_cap() -> None:
    """max_concurrency bounds simultaneous searches per client (plus the shared Redis slot when configured)."""
    import asyncio

    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json=_annotated(1))

    client = OpenRouterWebSearchClient(
        api_key=KEY, transport=httpx.MockTransport(handler), max_concurrency=2, retry_delay=0
    )
    with contextlib.suppress(Exception):
        await asyncio.gather(*(client.search(f"q{i}") for i in range(6)))
    assert peak <= 2
