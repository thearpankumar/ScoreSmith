"""OpenRouter client / web-search / settings tests. Pure unit tests (httpx.MockTransport) —
no network and no database access."""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest

from app.ai import openrouter_client as orc
from app.ai.bedrock_client import BedrockTimeoutError, BedrockUnavailableError, ToolSpec
from app.ai.openrouter_client import OpenRouterClient
from app.ai.web_search import OpenRouterWebSearchClient, extract_openrouter_results
from app.config import Settings, get_settings

KEY = "sk-or-v1-supersecretkey123"
TOOL = ToolSpec(name="emit", description="Emit result", input_schema={"type": "object", "properties": {}})
MSGS = [{"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}]


def _chat(args="{}", finish="tool_calls", content=None, tool=True):
    message: dict = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": "emit", "arguments": args}}]
    return {"choices": [{"finish_reason": finish, "message": message}]}


def _client(handler, **kw) -> OpenRouterClient:
    return OpenRouterClient(api_key=KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None, **kw)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    orc._NO_FORCED_TOOL_CHOICE.clear()
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_reasoning_effort", "low")
    monkeypatch.setattr(s, "bedrock_max_output_tokens", 16000)
    yield
    orc._NO_FORCED_TOOL_CHOICE.clear()


def test_payload_shape_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat('{"a": 1}'))

    res = _client(handler).converse(
        messages=MSGS, system="sys", tools=[TOOL], force_tool_use=True, model_id="openai/gpt-6-luna"
    )
    body = seen["body"]
    assert seen["url"].endswith("/chat/completions")
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hello\nworld"}
    assert body["tools"][0] == {
        "type": "function",
        "function": {"name": "emit", "description": "Emit result", "parameters": TOOL.input_schema},
    }
    assert body["tool_choice"] == "required"
    assert body["parallel_tool_calls"] is False
    assert body["provider"] == {"require_parameters": True}
    assert body["max_tokens"] == 16000
    assert body["reasoning"] == {"effort": "low"}
    assert "temperature" not in body
    assert seen["headers"]["authorization"] == f"Bearer {KEY}"
    assert "http-referer" in seen["headers"]
    assert "x-openrouter-title" in seen["headers"] and "x-title" in seen["headers"]
    # JSON-string arguments are parsed
    assert res.is_tool_use and res.tool_name == "emit" and res.tool_input == {"a": 1}
    assert res.tool_use_id == "call_1" and res.stop_reason == "tool_use" and not res.truncated


def test_temperature_only_when_set_and_reasoning_omitted_when_empty(monkeypatch):
    monkeypatch.setattr(get_settings(), "openrouter_reasoning_effort", "")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_chat(content="hi", tool=False, finish="stop"))

    c = _client(handler)
    r = c.converse(messages=MSGS, temperature=0)
    c.converse(messages=MSGS)
    assert bodies[0]["temperature"] == 0
    assert "temperature" not in bodies[1]
    assert "reasoning" not in bodies[0]
    assert r.text == "hi" and r.stop_reason == "end_turn" and not r.is_tool_use


def test_per_model_max_tokens_clamp(monkeypatch):
    monkeypatch.setattr(get_settings(), "bedrock_max_output_tokens", 500000)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_chat(content="x", tool=False, finish="stop"))

    c = _client(handler)
    c.converse(messages=MSGS, model_id="openai/gpt-6-luna")
    assert bodies[0]["max_tokens"] == 128000


def test_forced_tool_choice_falls_back_and_is_remembered():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tool_choice") == "required":
            return httpx.Response(400, json={"error": {"message": "tool_choice required is not supported"}})
        return httpx.Response(200, json=_chat('{"ok": true}'))

    c = _client(handler)
    res = c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True, model_id="m/x")
    assert res.tool_input == {"ok": True}
    assert [b["tool_choice"] for b in bodies] == ["required", "auto"]
    assert "m/x" in orc._NO_FORCED_TOOL_CHOICE
    c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True, model_id="m/x")
    assert len(bodies) == 3 and bodies[2]["tool_choice"] == "auto"


def test_require_parameters_and_max_tokens_fallbacks():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "provider" in body:
            msg = "No endpoints found that can handle the requested parameters"
            return httpx.Response(404, json={"error": {"message": msg}})
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: max_tokens"}})
        return httpx.Response(200, json=_chat('{"z": 2}'))

    res = _client(handler).converse(messages=MSGS, tools=[TOOL], force_tool_use=True)
    assert res.tool_input == {"z": 2}
    assert "provider" not in bodies[-1] and "max_tokens" not in bodies[-1]


def test_length_finish_is_truncated():
    c = _client(lambda r: httpx.Response(200, json=_chat('{"a": [1, 2', finish="length")))
    res = c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True)
    assert res.truncated and res.stop_reason == "max_tokens"

    c2 = _client(lambda r: httpx.Response(200, json=_chat(content="partial", tool=False, finish="length")))
    assert c2.converse(messages=MSGS).truncated


def test_malformed_arguments_treated_as_truncated():
    c = _client(lambda r: httpx.Response(200, json=_chat('{"a": ', finish="tool_calls")))
    res = c.converse(messages=MSGS, tools=[TOOL])
    assert res.truncated and res.tool_input == {}


def test_list_content_is_joined():
    data = _chat(tool=False, finish="stop", content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    res = _client(lambda r: httpx.Response(200, json=data)).converse(messages=MSGS)
    assert res.text == "a\nb"


def test_429_message_marks_throttle():
    from app.pipeline.master import _is_throttle

    c = _client(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert _is_throttle(ei.value) and not isinstance(ei.value, BedrockTimeoutError)


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_and_credit_errors_clear_and_secret_free(status):
    c = _client(lambda r: httpx.Response(status, json={"error": {"message": f"bad key {KEY}"}}))
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert KEY not in str(ei.value) and str(status) in str(ei.value)


def test_other_error_message_scrubs_key():
    c = _client(lambda r: httpx.Response(400, json={"error": {"message": f"nope {KEY}"}}))
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert KEY not in str(ei.value) and "***" in str(ei.value)


def test_timeouts_map_to_timeout_error():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(BedrockTimeoutError):
        _client(boom).converse(messages=MSGS)
    with pytest.raises(BedrockTimeoutError):
        _client(lambda r: httpx.Response(504, text="gateway")).converse(messages=MSGS)
    with pytest.raises(BedrockTimeoutError):
        _client(lambda r: httpx.Response(408, text="req")).converse(messages=MSGS)


def test_502_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, headers={"retry-after": "0"}, json={"error": {"message": "upstream"}})
        return httpx.Response(200, json=_chat(content="ok", tool=False, finish="stop"))

    assert _client(handler).converse(messages=MSGS).text == "ok"
    assert len(calls) == 2


def test_error_object_in_200_body_is_raised():
    c = _client(lambda r: httpx.Response(200, json={"error": {"code": 429, "message": "rate limited"}}))
    with pytest.raises(BedrockUnavailableError, match="(?i)throttl"):
        c.converse(messages=MSGS)


def test_missing_key_never_hits_network():
    def handler(request):
        raise AssertionError("no request expected")

    c = OpenRouterClient(api_key="", transport=httpx.MockTransport(handler))
    with pytest.raises(BedrockUnavailableError, match="OPENROUTER_API_KEY"):
        c.converse(messages=MSGS)


def test_stream_not_implemented():
    with pytest.raises(NotImplementedError):
        _client(lambda r: httpx.Response(200)).converse_stream(messages=MSGS)


def test_embed_normalises_and_sends_dimensions():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0] + [0.0] * 1022}]})

    vec = _client(handler).embed("hello")
    assert seen["url"].endswith("/embeddings")
    assert seen["body"]["dimensions"] == 1024 and seen["body"]["model"] == "openai/text-embedding-3-small"
    assert len(vec) == 1024 and math.isclose(sum(x * x for x in vec), 1.0)
    assert math.isclose(vec[0], 0.6)


def test_embed_wrong_dimension_rejected():
    c = _client(lambda r: httpx.Response(200, json={"data": [{"embedding": [1.0] * 10}]}))
    with pytest.raises(BedrockUnavailableError, match="dimensions"):
        c.embed("x")


# --- web search ------------------------------------------------------------------------------


def _annotated(n: int) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": "summary",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": f"https://example.com/{i}",
                                "title": "T" * 300,
                                "content": "C" * 900,
                            },
                        }
                        for i in range(n)
                    ]
                    + [{"type": "other"}],
                }
            }
        ]
    }


def test_extract_openrouter_results_truncates_and_caps():
    results = extract_openrouter_results(_annotated(15))
    assert len(results) == 10
    assert len(results[0].title) == 200 and len(results[0].snippet) == 500
    assert results[0].url == "https://example.com/0"
    assert extract_openrouter_results({}) == []
    assert extract_openrouter_results({"choices": [{"message": {"annotations": "x"}}]}) == []


def test_web_search_client_request_and_results():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=_annotated(3))

    c = OpenRouterWebSearchClient(
        api_key=KEY, engine="exa", max_results=5, transport=httpx.MockTransport(handler), backoff=False
    )
    results = asyncio.run(c.search("kpis for support"))
    assert len(results) == 3
    assert seen["body"]["plugins"] == [{"id": "web", "max_results": 5, "engine": "exa"}]
    assert seen["body"]["model"] == "openai/gpt-6-luna"
    assert seen["auth"] == f"Bearer {KEY}"


def test_web_search_engine_omitted_by_default_and_never_raises():
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(500, text="boom")

    c = OpenRouterWebSearchClient(api_key=KEY, transport=httpx.MockTransport(handler), backoff=False)
    assert asyncio.run(c.search("q")) == []
    assert len(bodies) == 3 and "engine" not in bodies[0]["plugins"][0]
    assert asyncio.run(OpenRouterWebSearchClient(api_key="").search("q")) == []
    assert not OpenRouterWebSearchClient(api_key="").is_configured


# --- settings ----------------------------------------------------------------------------------


def test_or_env_aliases_and_no_fallback_to_old_names(monkeypatch):
    for old in ("S3_BUCKET", "SFN_STATE_MACHINE_ARN", "AWS_APP_ACCESS_KEY_ID", "AWS_APP_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(old, "old-" + old)
    s = Settings(_env_file=None)
    assert (s.s3_bucket, s.sfn_state_machine_arn, s.aws_app_access_key_id, s.aws_app_secret_access_key) == ("",) * 4
    monkeypatch.setenv("OR_S3_BUCKET", "new-bucket")
    monkeypatch.setenv("OR_SFN_STATE_MACHINE_ARN", "arn:new")
    monkeypatch.setenv("OR_AWS_APP_ACCESS_KEY_ID", "AKIANEW")
    monkeypatch.setenv("OR_AWS_APP_SECRET_ACCESS_KEY", "secretnew")
    s = Settings(_env_file=None)
    assert s.s3_bucket == "new-bucket" and s.sfn_state_machine_arn == "arn:new"
    assert s.aws_app_access_key_id == "AKIANEW" and s.aws_app_secret_access_key == "secretnew"
    # construction by field name still works
    assert Settings(_env_file=None, s3_bucket="x").s3_bucket == "x"


def test_provider_neutral_model_accessors_and_jev_key_fallback():
    s = Settings(_env_file=None, openrouter_api_key="k1", openrouter_jev_api="")
    assert s.chat_model_id == "openai/gpt-6-luna" and s.judge_model_id == "deepseek/deepseek-v4.1-flash"
    assert s.master_model_id == "openai/gpt-6-luna"
    assert s.embedding_model_id == "openai/text-embedding-3-small"
    assert s.jev_api_key == "k1"
    assert Settings(_env_file=None, openrouter_api_key="k1", openrouter_jev_api="j").jev_api_key == "j"
    b = Settings(_env_file=None, llm_provider="bedrock")
    assert b.chat_model_id == b.bedrock_chat_model_id and b.embedding_model_id == b.bedrock_embedding_model_id
