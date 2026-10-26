"""OpenRouter chat / embeddings client (app/ai/openrouter_client.py). Pure unit tests over `httpx.MockTransport`:
no network, no database."""

from __future__ import annotations

import json
import math

import httpx
import pytest

from app.ai import openrouter_client as orc
from app.ai.bedrock_client import (
    BedrockClient,
    BedrockTimeoutError,
    BedrockUnavailableError,
    LLMTimeoutError,
    LLMUnavailableError,
    ToolSpec,
)
from app.ai.openrouter_client import OpenRouterClient
from app.config import get_settings
from app.pipeline.master import _is_throttle

KEY = "sk-or-v1-test123"  # short on purpose: tests/test_secrets_guard.py flags realistic keys
TOOL = ToolSpec(name="emit", description="Emit result", input_schema={"type": "object", "properties": {}})
OTHER = ToolSpec(name="other", description="Other", input_schema={"type": "object", "properties": {}})
MSGS = [{"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}]
GPT = "openai/gpt-5-mini"


def _chat(args="{}", finish="tool_calls", content=None, tool=True, name="emit"):
    message: dict = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": args}}]
    return {"provider": "OpenAI", "choices": [{"finish_reason": finish, "message": message}]}


def _text(content="ok", finish="stop"):
    return _chat(content=content, tool=False, finish=finish)


class Recorder:
    """A MockTransport handler that answers from a list of responses (the last one repeats) and keeps the bodies."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[dict] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content) if request.content else {})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _client(handler, **kw) -> tuple[OpenRouterClient, list[float]]:
    slept: list[float] = []
    c = OpenRouterClient(api_key=KEY, transport=httpx.MockTransport(handler), sleep=slept.append, **kw)
    return c, slept


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    orc.reset_learned_rejections()
    s = get_settings()
    for name, value in {
        "openrouter_reasoning": "low",
        "openrouter_max_output_tokens": 32000,
        "openrouter_provider_sort": "latency",
        "openrouter_provider_ignore": "",
        "openrouter_max_attempts": 2,
        "openrouter_app_title": "ScoreSmith",
        "openrouter_http_referer": "https://scoresmith.local",
        "openrouter_agent_model": GPT,
        "openrouter_embed_model": "openai/text-embedding-3-small",
        "openrouter_api_base": "https://openrouter.ai/api/v1",
    }.items():
        monkeypatch.setattr(s, name, value)
    yield
    orc.reset_learned_rejections()


# --- request shape --------------------------------------------------------------------------------------------


def test_payload_shape_headers_and_named_tool_choice() -> None:
    rec = Recorder(httpx.Response(200, json=_chat('{"a": 1}')))
    c, _ = _client(rec)
    res = c.converse(messages=MSGS, system="sys", tools=[TOOL], force_tool_use=True)
    body, req = rec.bodies[0], rec.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert body["model"] == GPT
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello\nworld"}]
    function = {"name": "emit", "description": "Emit result", "parameters": TOOL.input_schema}
    assert body["tools"] == [{"type": "function", "function": function}]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "emit"}}
    assert "parallel_tool_calls" not in body
    assert body["provider"] == {"require_parameters": True, "sort": "latency"}
    assert body["max_tokens"] == 32000
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert "temperature" not in body and "top_p" not in body and "seed" not in body
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-openrouter-title"] == "ScoreSmith"
    assert req.headers["http-referer"] == "https://scoresmith.local"
    assert res.is_tool_use and res.tool_name == "emit" and res.tool_input == {"a": 1}
    assert res.tool_use_id == "call_1" and res.stop_reason == "tool_use" and not res.truncated


def test_tool_choice_required_for_several_tools_and_auto_when_not_forced() -> None:
    rec = Recorder(httpx.Response(200, json=_chat()))
    c, _ = _client(rec)
    c.converse(messages=MSGS, tools=[TOOL, OTHER], force_tool_use=True)
    c.converse(messages=MSGS, tools=[TOOL], force_tool_use=False)
    c.converse(messages=MSGS)
    assert rec.bodies[0]["tool_choice"] == "required"
    assert rec.bodies[1]["tool_choice"] == "auto"
    assert "tools" not in rec.bodies[2] and "tool_choice" not in rec.bodies[2]


def test_temperature_is_stripped_for_gpt5_and_seed_replaces_zero() -> None:
    rec = Recorder(httpx.Response(200, json=_text()))
    c, _ = _client(rec)
    c.converse(messages=MSGS, temperature=0)
    c.converse(messages=MSGS, temperature=0.7)
    c.converse(messages=MSGS, model_id="openai/o4-mini", temperature=0)
    c.converse(messages=MSGS, model_id="mistralai/mistral-small", temperature=0)
    c.converse(messages=MSGS, model_id="mistralai/mistral-small", temperature=0.5)
    c.converse(messages=MSGS, model_id="mistralai/mistral-small")
    b = rec.bodies
    assert "temperature" not in b[0] and b[0]["seed"] == orc.DEFAULT_SEED
    assert "temperature" not in b[1] and "seed" not in b[1]
    assert "temperature" not in b[2] and b[2]["seed"] == orc.DEFAULT_SEED
    assert b[3]["temperature"] == 0 and b[3]["seed"] == orc.DEFAULT_SEED
    assert b[4]["temperature"] == 0.5 and "seed" not in b[4]
    assert "temperature" not in b[5] and "seed" not in b[5]
    assert all("top_p" not in body for body in b)


def test_reasoning_sort_and_max_tokens_can_be_turned_off(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_reasoning", "")
    monkeypatch.setattr(s, "openrouter_provider_sort", "")
    monkeypatch.setattr(s, "openrouter_max_output_tokens", 0)
    rec = Recorder(httpx.Response(200, json=_text()))
    c, _ = _client(rec)
    c.converse(messages=MSGS)
    assert "reasoning" not in rec.bodies[0] and "max_tokens" not in rec.bodies[0]
    assert rec.bodies[0]["provider"] == {"require_parameters": True}
    monkeypatch.setattr(s, "openrouter_reasoning", "medium")
    c.converse(messages=MSGS)
    assert rec.bodies[1]["reasoning"] == {"effort": "medium", "exclude": True}


def test_messages_unknown_roles_become_user_and_trailing_assistant_gets_continue() -> None:
    rec = Recorder(httpx.Response(200, json=_text()))
    c, _ = _client(rec)
    c.converse(messages=[{"role": "tool", "content": "x"}, {"role": "assistant", "content": [{"text": "Draft:"}]}])
    assert rec.bodies[0]["messages"] == [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "Draft:"},
        {"role": "user", "content": "Continue."},
    ]


def test_bedrock_model_id_falls_back_to_the_agent_model() -> None:
    rec = Recorder(httpx.Response(200, json=_text()))
    c, _ = _client(rec)
    c.converse(messages=MSGS, model_id="zai.glm-4.7-flash")
    c.converse(messages=MSGS, model_id="anthropic/claude-x")
    assert rec.bodies[0]["model"] == GPT and rec.bodies[1]["model"] == "anthropic/claude-x"


def test_bedrock_client_ignores_an_openrouter_model_id() -> None:
    b = BedrockClient()
    assert b.provider == "bedrock" and b.master_model_id == get_settings().bedrock_master_model_id
    assert b._model("openai/gpt-5-mini") == b.chat_model_id
    assert b._model("zai.glm-4.7-flash") == "zai.glm-4.7-flash"
    assert b._model(None) == b.chat_model_id
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc"
    assert b._model(arn) == arn
    assert LLMUnavailableError is BedrockUnavailableError and LLMTimeoutError is BedrockTimeoutError


def test_client_attributes_and_key_read_at_call_time(monkeypatch) -> None:
    c = OpenRouterClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_text())))
    assert c.provider == "openrouter"
    assert (c.chat_model_id, c.judge_model_id, c.master_model_id) == (GPT, GPT, GPT)
    assert c.embedding_model_id == "openai/text-embedding-3-small"
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")
    with pytest.raises(BedrockUnavailableError, match="OPENROUTER_API_KEY"):
        c.converse(messages=MSGS)
    monkeypatch.setattr(get_settings(), "openrouter_api_key", "k-later")
    assert c.converse(messages=MSGS).text == "ok"


def test_missing_key_never_hits_the_network() -> None:
    def handler(request):
        raise AssertionError("no request expected")

    c = OpenRouterClient(api_key="", transport=httpx.MockTransport(handler))
    with pytest.raises(BedrockUnavailableError, match="OPENROUTER_API_KEY"):
        c.converse(messages=MSGS)
    with pytest.raises(BedrockUnavailableError, match="OPENROUTER_API_KEY"):
        c.embed("x")


# --- response parsing -----------------------------------------------------------------------------------------


def test_tool_calls_with_finish_stop_count_as_tool_use() -> None:
    c, _ = _client(Recorder(httpx.Response(200, json=_chat('{"b": 2}', finish="stop"))))
    res = c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True)
    assert res.stop_reason == "tool_use" and res.tool_input == {"b": 2}


def test_length_finish_and_malformed_arguments_are_truncated() -> None:
    c, _ = _client(Recorder(httpx.Response(200, json=_chat('{"a": [1, 2', finish="length"))))
    res = c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True)
    assert res.truncated and res.stop_reason == "max_tokens"
    c2, _ = _client(Recorder(httpx.Response(200, json=_text("partial", finish="length"))))
    assert c2.converse(messages=MSGS).truncated
    c3, _ = _client(Recorder(httpx.Response(200, json=_chat('{"a": ', finish="tool_calls"))))
    res3 = c3.converse(messages=MSGS, tools=[TOOL])
    # no half-parsed tool call reaches the caller: guards like `not result.is_tool_use` take their failure path
    assert res3.truncated and not res3.is_tool_use and res3.tool_input is None
    c4, _ = _client(Recorder(httpx.Response(200, json=_chat("[1, 2]", finish="tool_calls"))))
    assert c4.converse(messages=MSGS, tools=[TOOL]).truncated


def test_list_content_is_joined_and_empty_text_is_none() -> None:
    data = _text(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    c, _ = _client(Recorder(httpx.Response(200, json=data)))
    assert c.converse(messages=MSGS).text == "a\nb"
    c2, _ = _client(Recorder(httpx.Response(200, json=_text(content=""))))
    res = c2.converse(messages=MSGS)
    assert res.text is None and res.stop_reason == "end_turn" and not res.is_tool_use


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": 500, "message": "upstream exploded"}},
        {"choices": [{"error": {"code": 500, "message": "mid-stream failure"}, "finish_reason": "error"}]},
        {"choices": [{"finish_reason": "error", "message": {"role": "assistant", "content": "half"}}]},
        {"choices": []},
    ],
)
def test_200_carrying_an_error_is_unavailable(body) -> None:
    c, _ = _client(Recorder(httpx.Response(200, json=body)))
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert not isinstance(ei.value, BedrockTimeoutError)


def test_200_with_a_throttle_error_is_retried_once_then_marked_throttled() -> None:
    rec = Recorder(httpx.Response(200, json={"error": {"code": 429, "message": "rate limited"}}))
    c, slept = _client(rec)
    with pytest.raises(BedrockUnavailableError, match="(?i)throttl"):
        c.converse(messages=MSGS)
    assert len(rec.bodies) == 2 and len(slept) == 1


# --- retries and errors ---------------------------------------------------------------------------------------


def test_429_is_retried_once_honouring_retry_after() -> None:
    rec = Recorder(
        httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "slow down"}}),
        httpx.Response(200, json=_text("fine")),
    )
    c, slept = _client(rec)
    assert c.converse(messages=MSGS).text == "fine"
    assert slept == [3.0] and len(rec.bodies) == 2


def test_429_twice_raises_a_throttle_error() -> None:
    rec = Recorder(httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}}))
    c, slept = _client(rec)
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert _is_throttle(ei.value) and "too many requests" in str(ei.value)
    assert len(rec.bodies) == 2 and slept == [0.0]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_and_credit_errors_fail_at_once_without_the_key(status) -> None:
    rec = Recorder(httpx.Response(status, json={"error": {"message": f"bad key {KEY}"}}))
    c, slept = _client(rec)
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert KEY not in str(ei.value) and str(status) in str(ei.value)
    assert len(rec.bodies) == 1 and slept == []


def test_402_from_the_in_flight_budget_is_retried_once() -> None:
    budget = {"error": {"code": 402, "message": "busy", "metadata": {"limit_source": "openrouter_in_flight_budget"}}}
    rec = Recorder(httpx.Response(402, json=budget), httpx.Response(200, json=_text("later")))
    c, slept = _client(rec)
    assert c.converse(messages=MSGS).text == "later"
    assert len(rec.bodies) == 2 and len(slept) == 1


def test_402_from_the_in_flight_budget_twice_is_reported_as_throttling() -> None:
    from app.pipeline.master import _is_throttle

    budget = {"error": {"code": 402, "message": "busy", "metadata": {"limit_source": "openrouter_in_flight_budget"}}}
    c, _ = _client(Recorder(httpx.Response(402, json=budget), httpx.Response(402, json=budget)))
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert _is_throttle(ei.value) and "Top up" not in str(ei.value)


def test_502_is_retried_with_backoff_then_gives_up() -> None:
    rec = Recorder(httpx.Response(502, json={"error": {"message": "upstream"}}), httpx.Response(200, json=_text("ok")))
    c, slept = _client(rec)
    assert c.converse(messages=MSGS).text == "ok"
    assert len(rec.bodies) == 2 and len(slept) == 1

    rec2 = Recorder(httpx.Response(503, json={"error": {"message": "overloaded"}}))
    c2, slept2 = _client(rec2)
    with pytest.raises(BedrockUnavailableError, match="503"):
        c2.converse(messages=MSGS)
    assert len(rec2.bodies) == 2 and len(slept2) == 1  # OPENROUTER_MAX_ATTEMPTS = 2


def test_connection_errors_are_retried_then_unavailable() -> None:
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError(f"refused {KEY}", request=request)

    c, slept = _client(handler)
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert len(calls) == 2 and len(slept) == 1 and KEY not in str(ei.value)


def test_timeouts_map_to_timeout_error() -> None:
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(BedrockTimeoutError):
        _client(boom)[0].converse(messages=MSGS)
    with pytest.raises(BedrockTimeoutError):
        _client(Recorder(httpx.Response(504, text="gateway")))[0].converse(messages=MSGS)
    with pytest.raises(BedrockTimeoutError):
        _client(Recorder(httpx.Response(408, text="request timeout")))[0].converse(messages=MSGS)


def test_error_messages_never_contain_the_key() -> None:
    c, _ = _client(Recorder(httpx.Response(400, json={"error": {"message": f"nope {KEY}"}})))
    with pytest.raises(BedrockUnavailableError) as ei:
        c.converse(messages=MSGS)
    assert KEY not in str(ei.value) and "***" in str(ei.value)
    assert orc.scrub(f"x sk-or-v1-{'a' * 30} y") == "x sk-or-*** y"


# --- remembered parameter rejections ----------------------------------------------------------------------------


def test_a_named_parameter_rejection_is_dropped_and_remembered() -> None:
    def handler(request):
        body = json.loads(request.content)
        if "seed" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'seed'"}})
        return httpx.Response(200, json=_text())

    rec_bodies = []

    def recording(request):
        rec_bodies.append(json.loads(request.content))
        return handler(request)

    c, _ = _client(recording)
    c.converse(messages=MSGS, model_id="mistralai/mistral-small", temperature=0)
    assert [("seed" in b) for b in rec_bodies] == [True, False]
    assert rec_bodies[1]["temperature"] == 0
    c.converse(messages=MSGS, model_id="mistralai/mistral-small", temperature=0)
    assert len(rec_bodies) == 3 and "seed" not in rec_bodies[2]  # remembered: no failed first try


def test_no_endpoints_404_drops_sampling_params_once_and_remembers() -> None:
    no_endpoints = {"error": {"code": 404, "message": "No endpoints found that can handle the requested parameters."}}
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body or "seed" in body:
            return httpx.Response(404, json=no_endpoints)
        return httpx.Response(200, json=_text())

    c, _ = _client(handler)
    assert c.converse(messages=MSGS, model_id="vendor/model", temperature=0).text == "ok"
    assert len(bodies) == 2 and "temperature" not in bodies[1] and "seed" not in bodies[1]
    assert bodies[1]["provider"]["require_parameters"] is True
    c.converse(messages=MSGS, model_id="vendor/model", temperature=0)
    assert len(bodies) == 3 and "temperature" not in bodies[2]


def test_no_endpoints_relaxes_require_parameters_once_and_never_loops() -> None:
    no_endpoints = {"error": {"code": 404, "message": "No endpoints found that can handle the requested parameters."}}
    rec = Recorder(httpx.Response(404, json=no_endpoints))
    c, _ = _client(rec)
    with pytest.raises(BedrockUnavailableError, match="404"):
        c.converse(messages=MSGS, model_id="vendor/model", temperature=0, tools=[TOOL], force_tool_use=True)
    assert len(rec.bodies) == 3  # as sent, without temperature/seed, without require_parameters
    assert "seed" in rec.bodies[0] and "seed" not in rec.bodies[1]
    assert rec.bodies[1]["provider"] == {"require_parameters": True, "sort": "latency"}
    assert rec.bodies[2]["provider"] == {"sort": "latency"}


def test_503_no_endpoints_is_a_routing_rejection_not_an_outage() -> None:
    rec = Recorder(
        httpx.Response(503, json={"error": {"message": "No endpoints found matching your data policy"}}),
        httpx.Response(200, json=_text()),
    )
    c, slept = _client(rec)
    assert c.converse(messages=MSGS).text == "ok"
    assert slept == [] and "require_parameters" not in rec.bodies[1].get("provider", {})


def test_named_tool_choice_rejection_falls_back_to_required_then_auto() -> None:
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tool_choice") not in ("auto", None):
            return httpx.Response(400, json={"error": {"message": f"tool_choice {body['tool_choice']} unsupported"}})
        return httpx.Response(200, json=_chat('{"ok": true}'))

    c, _ = _client(handler)
    res = c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True, model_id="m/x")
    assert res.tool_input == {"ok": True}
    named = {"type": "function", "function": {"name": "emit"}}
    assert [b["tool_choice"] for b in bodies] == [named, "required", "auto"]
    c.converse(messages=MSGS, tools=[TOOL], force_tool_use=True, model_id="m/x")
    assert bodies[-1]["tool_choice"] == "auto" and len(bodies) == 4


def test_converse_stream_replays_one_completion() -> None:
    c, _ = _client(Recorder(httpx.Response(200, json=_text("streamed"))))
    events = list(c.converse_stream(messages=MSGS))
    assert events == [
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "streamed"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]


# --- embeddings -----------------------------------------------------------------------------------------------


def test_embed_sends_dimensions_and_normalises() -> None:
    rec = Recorder(httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0] + [0.0] * 1022}], "provider": "OpenAI"}))
    c, _ = _client(rec)
    vec = c.embed("hello")
    assert str(rec.requests[0].url).endswith("/embeddings")
    assert rec.bodies[0] == {
        "model": "openai/text-embedding-3-small", "input": "hello", "dimensions": 1024, "encoding_format": "float",
    }
    assert len(vec) == 1024 and math.isclose(sum(x * x for x in vec), 1.0) and math.isclose(vec[0], 0.6)


def test_embed_rejects_wrong_dimensions_zero_vectors_and_errors() -> None:
    c, _ = _client(Recorder(httpx.Response(200, json={"data": [{"embedding": [1.0] * 10}]})))
    with pytest.raises(BedrockUnavailableError, match="dimensions"):
        c.embed("x")
    c2, _ = _client(Recorder(httpx.Response(200, json={"data": [{"embedding": [0.0] * 1024}]})))
    with pytest.raises(BedrockUnavailableError, match="zero"):
        c2.embed("x")
    c3, _ = _client(Recorder(httpx.Response(401, json={"error": {"message": "no"}})))
    with pytest.raises(BedrockUnavailableError, match="401"):
        c3.embed("x")
    c4, _ = _client(Recorder(httpx.Response(200, json={"data": []})))
    with pytest.raises(BedrockUnavailableError, match="embedding"):
        c4.embed("x")


# --- chat agent: Claude 5.x quirks, rich content, several tool calls, reasoning passback --------------------------

CLAUDE = "anthropic/claude-sonnet-5.5"


def test_claude_5_is_never_forced_and_never_sent_sampling_params() -> None:
    rec = Recorder(httpx.Response(200, json=_chat()))
    c, _ = _client(rec)
    c.converse(messages=MSGS, system="sys", tools=[TOOL], force_tool_use=True, model_id=CLAUDE, temperature=0)
    body = rec.bodies[0]
    assert body["model"] == CLAUDE and body["tool_choice"] == "auto"
    assert "temperature" not in body
    # Forcing is asked for in words instead, after the caller's own system prompt.
    system = body["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("sys\n\n") and "calling exactly one" in system["content"]
    assert len(rec.bodies) == 1  # no wasted rejected call


def test_rich_messages_translate_tool_round_trips_files_and_cache_points() -> None:
    rec = Recorder(httpx.Response(200, json=_text("done")))
    c, _ = _client(rec)
    messages = [
        {"role": "user", "content": [
            {"text": "files:"},
            {"document": {"format": "pdf", "name": "sop.pdf", "source": {"bytes": b"%PDF-1"}}},
            {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}},
            {"cachePoint": {"type": "default"}},
        ]},
        {"role": "assistant", "content": [
            {"text": "Let me look."},
            {"toolUse": {"toolUseId": "t1", "name": "web_search", "input": {"query": "csat"}}},
            {"toolUse": {"toolUseId": "t2", "name": "view_kpis", "input": {"names": ["A"]}}},
            {"reasoningDetails": [{"type": "reasoning.encrypted", "data": "abc"}]},
        ]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": "results"}]}},
            {"toolResult": {"toolUseId": "t2", "content": [{"text": "kpis"}]}},
        ]},
    ]
    c.converse(messages=messages, tools=[TOOL], model_id=CLAUDE)
    sent = rec.bodies[0]["messages"]
    user = sent[0]
    assert user["role"] == "user" and user["content"][0] == {"type": "text", "text": "files:"}
    assert user["content"][1] == {
        "type": "file", "file": {"filename": "sop.pdf", "file_data": "data:application/pdf;base64,JVBERi0x"},
    }
    assert user["content"][2]["type"] == "image_url"
    assert user["content"][2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert user["content"][2]["cache_control"] == {"type": "ephemeral"}  # the part before the cache point
    assistant = sent[1]
    assert assistant["content"] == "Let me look."
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["t1", "t2"]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"query": "csat"}
    assert assistant["reasoning_details"] == [{"type": "reasoning.encrypted", "data": "abc"}]
    assert sent[2] == {"role": "tool", "tool_call_id": "t1", "content": "results"}
    assert sent[3] == {"role": "tool", "tool_call_id": "t2", "content": "kpis"}
    assert len(sent) == 4  # tool results only: no trailing "Continue." and no empty user message


def test_cache_points_are_dropped_for_models_that_ignore_cache_control() -> None:
    rec = Recorder(httpx.Response(200, json=_text("done")))
    c, _ = _client(rec)
    c.converse(messages=[{"role": "user", "content": [{"text": "a"}, {"cachePoint": {"type": "default"}}]}])
    assert rec.bodies[0]["messages"] == [{"role": "user", "content": [{"type": "text", "text": "a"}]}]


def test_several_tool_calls_reasoning_details_and_usage_are_returned() -> None:
    body = _chat('{"query": "a"}', name="web_search")
    message = body["choices"][0]["message"]
    message["tool_calls"].append(
        {"id": "call_2", "type": "function", "function": {"name": "view_kpis", "arguments": '{"names": ["X"]}'}}
    )
    message["tool_calls"].append({"id": "call_3", "type": "function", "function": {"name": "cut", "arguments": "{"}})
    message["reasoning_details"] = [{"type": "reasoning.text", "text": "thinking"}]
    body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0012}
    rec = Recorder(httpx.Response(200, json=body))
    c, _ = _client(rec)
    res = c.converse(messages=MSGS, tools=[TOOL, OTHER], model_id=CLAUDE)
    assert res.tool_name == "web_search" and res.tool_input == {"query": "a"}
    assert [(t.id, t.name, t.input) for t in res.tool_calls] == [
        ("call_1", "web_search", {"query": "a"}), ("call_2", "view_kpis", {"names": ["X"]}),
    ]  # the cut-off third call is never acted on
    assert res.reasoning_details == [{"type": "reasoning.text", "text": "thinking"}]
    assert res.usage["cost"] == 0.0012


def test_keep_reasoning_effort_override_and_session_header() -> None:
    rec = Recorder(httpx.Response(200, json=_text()))
    c, _ = _client(rec)
    c.converse(messages=MSGS, model_id=CLAUDE, reasoning_effort="medium", keep_reasoning=True, session_key="s-1")
    assert rec.bodies[0]["reasoning"] == {"effort": "medium"}
    assert rec.requests[0].headers["x-session-id"] == "s-1"
    c.converse(messages=MSGS)
    assert rec.bodies[1]["reasoning"] == {"effort": "low", "exclude": True}
    assert "x-session-id" not in rec.requests[1].headers


def test_builder_tier_attributes_use_the_kpi_model(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "openrouter_kpi_model", "openai/gpt-5.6-luna")
    c = OpenRouterClient(api_key=KEY)
    assert c.builder_model_id == c.builder_judge_model_id == "openai/gpt-5.6-luna"
    assert c.judge_model_id == c.master_model_id == GPT  # evaluations keep the agent model


def test_ignored_endpoints_are_sent_with_the_routing(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "openrouter_provider_ignore", "openai/fast, azure ")
    rec = Recorder(httpx.Response(200, json=_text()))
    c, _ = _client(rec)
    c.converse(messages=MSGS, model_id="openai/gpt-5.6-luna")
    routing = rec.bodies[0]["provider"]
    assert routing == {"require_parameters": True, "sort": "latency", "ignore": ["openai/fast", "azure"]}
