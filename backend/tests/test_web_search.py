"""Tests for `app.ai.web_search` — the AgentCore Gateway web-search client.

No real network calls here (this build environment previously had no live Gateway
credentials at all when these tests were first written; even now that it does, the unit
suite stays fast/deterministic by monkeypatching `httpx.AsyncClient` rather than hitting
the real Gateway — the real Gateway is only exercised by the live end-to-end pass, not by
`pytest`). Covers: SigV4 header shape, MCP response parsing (including the documented
defensive shapes), "not configured" short-circuiting, and the retry/backoff-then-empty-
list failure contract.
"""

from __future__ import annotations

import json

import httpx

from app.ai.web_search import (
    AgentCoreWebSearchClient,
    _sigv4_headers,
    extract_results,
)

# No `pytestmark = pytest.mark.asyncio` needed — pyproject.toml sets
# `asyncio_mode = "auto"`, so async def tests below are picked up automatically; the
# sync tests in this file are plain pytest tests.

# --- SigV4 header shape -------------------------------------------------------------------


def test_sigv4_headers_have_expected_shape() -> None:
    headers = _sigv4_headers(
        method="POST",
        url="https://example.execute-api.us-east-1.amazonaws.com/mcp",
        body=b'{"hello": "world"}',
        region="us-east-1",
        access_key="AKIDEXAMPLE",
        secret_key="secretkey",
    )
    assert headers["host"] == "example.execute-api.us-east-1.amazonaws.com"
    assert headers["Content-Type"] == "application/json"
    assert headers["x-amz-date"].endswith("Z")
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "SignedHeaders=host;x-amz-date" in headers["Authorization"]
    assert "Signature=" in headers["Authorization"]


def test_sigv4_signature_changes_with_secret() -> None:
    kwargs = dict(
        method="POST",
        url="https://example.amazonaws.com/mcp",
        body=b"{}",
        region="us-east-1",
        access_key="AKID",
    )
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    sig_a = _sigv4_headers(**kwargs, secret_key="secret-a", now=now)["Authorization"]
    sig_b = _sigv4_headers(**kwargs, secret_key="secret-b", now=now)["Authorization"]
    assert sig_a != sig_b


# --- Response parsing (see module docstring: dict-with-results / bare list / single dict) -


def test_extract_results_dict_with_results_list() -> None:
    inner = {
        "id": "abc",
        "results": [
            {
                "title": "Incident Postmortem Best Practices",
                "url": "https://sre.google/sre-book/postmortem-culture/",
                "text": "Blameless postmortems reduce MTTR by...",
                "publishedDate": "2023-01-01",
            }
        ],
    }
    data = {"result": {"content": [{"text": json.dumps(inner)}]}}
    results = extract_results(data)
    assert len(results) == 1
    assert results[0].title == "Incident Postmortem Best Practices"
    assert results[0].url == "https://sre.google/sre-book/postmortem-culture/"
    assert results[0].published_date == "2023-01-01"


def test_extract_results_bare_list() -> None:
    inner = [{"title": "A", "url": "https://a.example", "text": "snippet a"}]
    data = {"result": {"content": [{"text": json.dumps(inner)}]}}
    results = extract_results(data)
    assert len(results) == 1 and results[0].title == "A"


def test_extract_results_single_dict() -> None:
    inner = {"title": "Solo", "url": "https://solo.example", "snippet": "just one"}
    data = {"result": {"content": [{"text": json.dumps(inner)}]}}
    results = extract_results(data)
    assert len(results) == 1 and results[0].title == "Solo"


def test_extract_results_caps_at_ten_and_truncates_fields() -> None:
    many = [{"title": "T" * 999, "url": "U" * 999, "text": "S" * 999} for _ in range(15)]
    data = {"result": {"content": [{"text": json.dumps({"results": many})}]}}
    results = extract_results(data)
    assert len(results) == 10
    assert len(results[0].title) == 200
    assert len(results[0].url) == 300
    assert len(results[0].snippet) == 500


def test_extract_results_malformed_shapes_return_empty_not_raise() -> None:
    assert extract_results({}) == []
    assert extract_results({"result": {}}) == []
    assert extract_results({"result": {"content": "not-a-list"}}) == []
    assert extract_results({"result": {"content": [{"text": "not json"}]}}) == []
    assert extract_results({"result": {"content": [{"no_text": True}]}}) == []


# --- Not configured -------------------------------------------------------------------


async def test_search_returns_empty_when_not_configured() -> None:
    client = AgentCoreWebSearchClient(url="", tool_name="", access_key="", secret_key="")
    assert client.is_configured is False
    results = await client.search("anything")
    assert results == []


# --- Retry/backoff + graceful empty-list-on-failure ------------------------------------


class _AlwaysFailsTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        raise httpx.ConnectTimeout("simulated network failure", request=request)


async def test_search_retries_then_returns_empty_on_persistent_failure(monkeypatch) -> None:
    transport = _AlwaysFailsTransport()
    real_async_client = httpx.AsyncClient  # captured BEFORE patching — see below

    def _fake_async_client(*args, **kwargs):
        # `httpx.AsyncClient` (module attribute) is what gets patched next, so this
        # closure must call the real class captured above, not `httpx.AsyncClient`
        # itself — calling that would recurse into this same fake indefinitely.
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.ai.web_search.httpx.AsyncClient", _fake_async_client)
    # Avoid real sleeping across the exponential backoff in the test.
    monkeypatch.setattr("app.ai.web_search.asyncio.sleep", _fast_sleep)

    client = AgentCoreWebSearchClient(
        url="https://gateway.example/mcp",
        tool_name="web_search",
        access_key="AKID",
        secret_key="secret",
        region="us-east-1",
    )
    results = await client.search("incident postmortem KPI benchmark")

    assert results == []  # graceful — never raises, worst case is []
    assert transport.call_count == 3  # exactly _MAX_ATTEMPTS retries, no more


class _SucceedsOnThirdTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if self.call_count < 3:
            raise httpx.ConnectTimeout("simulated transient failure", request=request)
        body = {
            "result": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "title": "Real result after retry",
                                        "url": "https://example.com/kpi",
                                        "text": "snippet",
                                    }
                                ]
                            }
                        )
                    }
                ]
            }
        }
        return httpx.Response(200, json=body, request=request)


async def test_search_recovers_after_transient_failures(monkeypatch) -> None:
    transport = _SucceedsOnThirdTransport()
    real_async_client = httpx.AsyncClient  # captured BEFORE patching — see the other test

    def _fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.ai.web_search.httpx.AsyncClient", _fake_async_client)
    monkeypatch.setattr("app.ai.web_search.asyncio.sleep", _fast_sleep)

    client = AgentCoreWebSearchClient(
        url="https://gateway.example/mcp",
        tool_name="web_search",
        access_key="AKID",
        secret_key="secret",
        region="us-east-1",
    )
    results = await client.search("query")
    assert len(results) == 1
    assert results[0].title == "Real result after retry"
    assert transport.call_count == 3


async def _fast_sleep(_seconds: float) -> None:
    return None
