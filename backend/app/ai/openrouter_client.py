"""OpenRouter implementation of `BedrockClientProtocol` (OpenAI-compatible chat completions and embeddings).

Used when an admin picks OpenRouter as the AI provider (app/ai/providers.py). The app keeps speaking the
Bedrock-Converse-shaped interface (`messages=[{"role", "content": [{"text": ...}]}]`, `ToolSpec`, `ConverseResult`);
this module translates it to `POST {OPENROUTER_API_BASE}/chat/completions` and `/embeddings` over httpx.

Request shape (defaults verified live against openai/gpt-5-mini, 2026-10-23):
- **Tool choice**: exactly one tool + `force_tool_use` -> a named-function `tool_choice`; several tools + force ->
  `"required"`; otherwise `"auto"`. `parallel_tool_calls` is never sent (gpt-5-mini rejects it).
- **Sampling**: `temperature` / `top_p` are never sent to `openai/gpt-5*` / `openai/o*` (they reject them, and with
  `provider.require_parameters` that surfaces as a bare HTTP 404 "No endpoints found..."). A call that asked for
  `temperature=0` (repeatable judging) sends `seed` instead.
- **Reasoning**: `{"effort": OPENROUTER_REASONING, "exclude": true}`; `max_tokens` = OPENROUTER_MAX_OUTPUT_TOKENS
  (reasoning tokens count against it).
- **Routing**: `provider: {"require_parameters": true, "sort": OPENROUTER_PROVIDER_SORT}`.
- When OpenRouter rejects a parameter for a model (a 400/404/422 naming it, or "no endpoints"), the parameter is
  dropped, remembered for that model for the life of the process, and the call retried; each fallback fires at
  most once per call, so a call never loops.

Errors are normalised to `BedrockUnavailableError` / `BedrockTimeoutError` (aliased as `LLMUnavailableError` /
`LLMTimeoutError`) so every existing handler keeps working. A 429 is retried once (honouring Retry-After) and then
raised with "throttled: too many requests" in the message, which `pipeline/master._is_throttle` and the AIMD
limiter key off. 502/503 are retried with backoff; 401/402/403 fail at once with a clear message. A 200 response
that carries an `error`, or a `finish_reason` of "error", counts as a failure too.

The API key is read at call time and never logged or put in an exception message (`scrub()`).
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, NoReturn

import httpx

from app.ai.bedrock_client import BedrockTimeoutError, BedrockUnavailableError, ConverseResult, ToolSpec
from app.config import get_settings

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = 1024
# Sent instead of temperature=0 to models that take no temperature, for repeatable answers.
DEFAULT_SEED = 7
_MAX_RETRY_AFTER = 30.0

# Models whose endpoints reject sampling parameters outright (OpenAI reasoning models).
_NO_SAMPLING_MODEL = re.compile(r"^openai/(gpt-5|o\d)")
# Optional parameters dropped (in this order) when OpenRouter finds no endpoint for the request.
_SAMPLING_PARAMS = ("temperature", "top_p", "seed")
# Parameters OpenRouter may name in a rejection; a named one is dropped and remembered for the model.
_DROPPABLE_PARAMS = ("temperature", "top_p", "seed", "max_tokens", "reasoning", "response_format")

# What each model rejected, learned for the life of the process (only the first call pays for the failed try).
_learned_lock = threading.Lock()
_DROPPED_PARAMS: dict[str, set[str]] = {}
_NO_NAMED_TOOL_CHOICE: set[str] = set()
_NO_FORCED_TOOL_CHOICE: set[str] = set()
_NO_REQUIRE_PARAMETERS: set[str] = set()

_FINISH_TO_STOP = {
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "content_filter": "content_filtered",
}

_KEY_PATTERN = re.compile(r"sk-or-[A-Za-z0-9_-]{6,}")


def reset_learned_rejections() -> None:
    """Forget every learned parameter rejection (tests)."""
    with _learned_lock:
        _DROPPED_PARAMS.clear()
        _NO_NAMED_TOOL_CHOICE.clear()
        _NO_FORCED_TOOL_CHOICE.clear()
        _NO_REQUIRE_PARAMETERS.clear()


def scrub(text: str, key: str | None = None) -> str:
    """`text` without the API key (or anything shaped like an OpenRouter key), cut to 500 characters."""
    if key:
        text = text.replace(key, "***")
    return _KEY_PATTERN.sub("sk-or-***", text)[:500]


class _HttpError(Exception):
    """Internal: a non-2xx response or a 200 that carries an `error`. Never escapes this module."""

    def __init__(
        self, status: int, message: str, retry_after: float | None = None, limit_source: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.limit_source = limit_source

    @property
    def flat(self) -> str:
        return self.message.lower().replace("_", " ")

    @property
    def no_endpoints(self) -> bool:
        flat = self.flat
        return "no endpoints" in flat or "require parameters" in flat or "requested parameters" in flat


class _TransportError(Exception):
    """Internal: the request never got an HTTP answer (connection refused / reset)."""


# --- translation ------------------------------------------------------------------------------------------------


def _flatten_text(content: Any) -> str:
    """Bedrock content blocks `[{"text": ...}]` (or a plain string) -> one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _tool_to_openai(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema},
    }


def _messages(messages: list[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role")
        out.append({"role": role if role in ("user", "assistant", "system") else "user",
                    "content": _flatten_text(m.get("content"))})
    if out and out[-1]["role"] == "assistant":
        # Bedrock models continue a trailing assistant message (prefill); GPT models have no prefill.
        out.append({"role": "user", "content": "Continue."})
    return out


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), _MAX_RETRY_AFTER))
    except ValueError:
        return None


def parse_chat_response(data: dict[str, Any]) -> ConverseResult:
    """A chat-completions body -> `ConverseResult`. Any tool call counts as tool use (even with finish "stop");
    finish "length" or unparseable tool arguments count as truncated ("max_tokens")."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices[0], dict):
        raise BedrockUnavailableError("OpenRouter response had no choices.")
    choice = choices[0]
    message = choice.get("message") or {}
    finish = str(choice.get("finish_reason") or choice.get("native_finish_reason") or "unknown").lower()
    if finish == "error":
        raise BedrockUnavailableError("OpenRouter chat completion failed: the provider ended the response in error.")
    stop_reason = _FINISH_TO_STOP.get(finish, finish)
    content = message.get("content")
    text = _flatten_text(content) if content is not None else None

    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    tool_calls = message.get("tool_calls") or []
    if isinstance(tool_calls, list) and tool_calls and isinstance(tool_calls[0], dict):
        call = tool_calls[0]
        fn = call.get("function") or {}
        tool_name = fn.get("name")
        tool_use_id = call.get("id")
        args = fn.get("arguments")
        if isinstance(args, dict):
            tool_input = args
        elif args is None or (isinstance(args, str) and not args.strip()):
            tool_input = {}
        else:
            try:
                parsed = json.loads(args)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                tool_input = parsed
            else:
                # Cut-off / malformed JSON: report "no tool call, truncated", so every caller takes its existing
                # missing-tool-call / retry-smaller path instead of reading keys out of a partial result.
                logger.warning("OpenRouter returned unparseable tool arguments (finish=%s).", finish)
                tool_name = tool_use_id = tool_input = None
                stop_reason = "max_tokens"
        if stop_reason not in ("max_tokens", "content_filtered"):
            stop_reason = "tool_use"
    return ConverseResult(
        stop_reason=stop_reason,
        text=text or None,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        raw=data,
    )


# --- client -----------------------------------------------------------------------------------------------------


class OpenRouterClient:
    """`BedrockClientProtocol` over OpenRouter. Synchronous (one shared `httpx.Client` pool), like boto3: async call
    sites wrap calls in `asyncio.to_thread` exactly as they do for Bedrock."""

    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        chat_model_id: str | None = None,
        judge_model_id: str | None = None,
        master_model_id: str | None = None,
        embedding_model_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key  # None = read OPENROUTER_API_KEY at call time
        self._base_url = base_url
        self.chat_model_id = chat_model_id or settings.openrouter_agent_model
        self.judge_model_id = judge_model_id or settings.openrouter_agent_model
        self.master_model_id = master_model_id or settings.openrouter_agent_model
        self.embedding_model_id = embedding_model_id or settings.openrouter_embed_model
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    # --- plumbing ---------------------------------------------------------------------------------------------

    def _key(self) -> str:
        key = self._api_key if self._api_key is not None else get_settings().openrouter_api_key
        return (key or "").strip()

    def _url(self, path: str) -> str:
        return (self._base_url or get_settings().openrouter_api_base).rstrip("/") + path

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    s = get_settings()
                    pool = max(s.bedrock_max_pool_connections, s.openrouter_max_concurrency * 2 + 8)
                    self._client = httpx.Client(
                        timeout=httpx.Timeout(
                            float(s.openrouter_read_timeout_seconds),
                            connect=float(s.openrouter_connect_timeout_seconds),
                        ),
                        limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool),
                        transport=self._transport,
                    )
        return self._client

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        s = get_settings()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if s.openrouter_http_referer:
            headers["HTTP-Referer"] = s.openrouter_http_referer
        if s.openrouter_app_title:
            headers["X-OpenRouter-Title"] = s.openrouter_app_title
            headers["X-Title"] = s.openrouter_app_title
        return headers

    def _model(self, model_id: str | None) -> str:
        """The OpenRouter model for a call. A Bedrock id (no "vendor/" prefix) reaching this client is a call-site
        bug (it resolved its model for the other provider): use the agent model instead of failing the call."""
        if model_id and "/" not in model_id:
            logger.warning(
                "OpenRouter client got a Bedrock model id %r; using %s instead.", model_id, self.chat_model_id
            )
            return self.chat_model_id
        return model_id or self.chat_model_id

    @staticmethod
    def _error_from_body(body: Any, fallback_status: int) -> _HttpError | None:
        """An `_HttpError` for a body that carries an `error` object (any HTTP status), else None."""
        if not isinstance(body, dict):
            return None
        err = body.get("error")
        if not err:
            return None
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            raw = meta.get("raw")
            if raw:
                message = f"{message} ({raw if isinstance(raw, str) else json.dumps(raw)[:300]})"
            code = err.get("code")
            status = int(code) if isinstance(code, int | float) and 100 <= int(code) < 600 else fallback_status
            limit_source = meta.get("limit_source") if isinstance(meta.get("limit_source"), str) else None
            return _HttpError(status, message.strip() or "error", limit_source=limit_source)
        return _HttpError(fallback_status, str(err))

    def _post(self, path: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
        """One POST. Returns the parsed body; raises `_HttpError` (non-2xx, or a 200 carrying an error),
        `_TransportError`, or `BedrockTimeoutError`."""
        try:
            response = self._get_client().post(self._url(path), json=payload, headers=self._headers(key))
        except httpx.TimeoutException as exc:
            raise BedrockTimeoutError(f"OpenRouter {path} call timed out ({type(exc).__name__}).") from None
        except httpx.HTTPError as exc:
            raise _TransportError(f"{type(exc).__name__}: {scrub(str(exc), key)}") from None
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            err = self._error_from_body(body, response.status_code) or _HttpError(
                response.status_code, (response.text or "")[:300]
            )
            err.status = response.status_code  # the HTTP status wins over an error code in the body
            err.retry_after = _parse_retry_after(response.headers.get("retry-after"))
            raise err
        if not isinstance(body, dict):
            raise BedrockUnavailableError(f"OpenRouter {path} returned a non-JSON response.")
        if not body.get("choices") and not body.get("data"):
            err = self._error_from_body(body, 502)
            if err is not None:
                raise err
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
            if first.get("error") and not first.get("message"):
                raise self._error_from_body({"error": first["error"]}, 502) or _HttpError(502, "error")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        logger.debug(
            "openrouter %s model=%s via=%s prompt=%s completion=%s reasoning=%s cost=%s",
            path, body.get("model"), body.get("provider"), usage.get("prompt_tokens"),
            usage.get("completion_tokens"), details.get("reasoning_tokens"), usage.get("cost"),
        )
        return body

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with the retry policy: a 429 (or a 402 from the in-flight budget) once, honouring Retry-After;
        502/503 and connection errors up to OPENROUTER_MAX_ATTEMPTS with backoff. Raises `_HttpError` for anything
        else (the caller maps it), `BedrockTimeoutError` / `BedrockUnavailableError` for transport failures."""
        key = self._key()
        if not key:
            raise BedrockUnavailableError("OpenRouter is not configured: OPENROUTER_API_KEY is not set.")
        attempts = max(1, get_settings().openrouter_max_attempts)
        throttled = failures = 0
        while True:
            try:
                return self._post(path, payload, key)
            except _HttpError as exc:
                if exc.status == 429 or (exc.status == 402 and exc.limit_source == "openrouter_in_flight_budget"):
                    if throttled >= 1:
                        raise
                    throttled += 1
                    delay = exc.retry_after if exc.retry_after is not None else 1.0 + random.uniform(0, 1)
                    logger.warning("OpenRouter %s throttled (HTTP %s); retrying in %.1fs.", path, exc.status, delay)
                    self._sleep(delay)
                    continue
                if exc.status in (502, 503) and not exc.no_endpoints:
                    failures += 1
                    if failures >= attempts:
                        raise
                    delay = exc.retry_after
                    if delay is None:
                        delay = 2 ** (failures - 1) + random.uniform(0, 0.5)
                    logger.warning("OpenRouter %s returned HTTP %s; retrying in %.1fs.", path, exc.status, delay)
                    self._sleep(delay)
                    continue
                raise
            except _TransportError as exc:
                failures += 1
                if failures >= attempts:
                    raise BedrockUnavailableError(f"OpenRouter {path} call failed: {exc}") from None
                logger.warning("OpenRouter %s connection failed (%s); retrying.", path, exc)
                self._sleep(2 ** (failures - 1) + random.uniform(0, 0.5))

    def _raise(self, operation: str, exc: _HttpError) -> NoReturn:
        status, detail = exc.status, scrub(exc.message, self._key())
        logger.error("OpenRouter %s failed with HTTP %s: %s", operation, status, detail)
        if status in (408, 504):
            raise BedrockTimeoutError(f"OpenRouter {operation} timed out (HTTP {status}): {detail}")
        if status == 429:
            raise BedrockUnavailableError(f"OpenRouter {operation} throttled: too many requests (HTTP 429): {detail}")
        if status == 402 and exc.limit_source == "openrouter_in_flight_budget":
            # A temporary cap on concurrent spend, not an empty account: worded so master._is_throttle backs off.
            raise BedrockUnavailableError(
                f"OpenRouter {operation} throttled: too many requests in flight for the key's budget (HTTP 402): "
                f"{detail}"
            )
        if status == 402:
            raise BedrockUnavailableError(
                f"OpenRouter {operation} failed: insufficient credits or budget (HTTP 402). "
                f"Top up the account: {detail}"
            )
        if status in (401, 403):
            raise BedrockUnavailableError(
                f"OpenRouter {operation} failed: authentication or permission error (HTTP {status}). "
                f"Check OPENROUTER_API_KEY and the key's model access: {detail}"
            )
        raise BedrockUnavailableError(f"OpenRouter {operation} failed (HTTP {status}): {detail}")

    # --- chat ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _tool_choice(model: str, tools: list[ToolSpec], force_tool_use: bool) -> Any:
        if force_tool_use and model not in _NO_FORCED_TOOL_CHOICE:
            if len(tools) == 1 and model not in _NO_NAMED_TOOL_CHOICE:
                return {"type": "function", "function": {"name": tools[0].name}}
            return "required"
        return "auto"

    def _chat_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        system: str | None,
        tools: list[ToolSpec] | None,
        force_tool_use: bool,
        temperature: float | None,
    ) -> dict[str, Any]:
        s = get_settings()
        dropped = _DROPPED_PARAMS.get(model, set())
        payload: dict[str, Any] = {"model": model, "messages": _messages(messages, system)}
        if tools:
            payload["tools"] = [_tool_to_openai(t) for t in tools]
            payload["tool_choice"] = self._tool_choice(model, tools, force_tool_use)
        if temperature is not None:
            if not _NO_SAMPLING_MODEL.match(model) and "temperature" not in dropped:
                payload["temperature"] = temperature
            if temperature == 0 and "seed" not in dropped:
                payload["seed"] = DEFAULT_SEED
        if s.openrouter_max_output_tokens > 0 and "max_tokens" not in dropped:
            payload["max_tokens"] = s.openrouter_max_output_tokens
        effort = (s.openrouter_reasoning or "").strip().lower()
        if effort and effort not in ("none", "off") and "reasoning" not in dropped:
            payload["reasoning"] = {"effort": effort, "exclude": True}
        routing: dict[str, Any] = {}
        if model not in _NO_REQUIRE_PARAMETERS:
            routing["require_parameters"] = True
        sort = (s.openrouter_provider_sort or "").strip()
        if sort:
            routing["sort"] = sort
        if routing:
            payload["provider"] = routing
        return payload

    @staticmethod
    def _apply_fallback(model: str, payload: dict[str, Any], exc: _HttpError, tried: set[str]) -> bool:
        """Adjust `payload` after a parameter / routing rejection; False when no (untried) fallback applies."""
        if exc.status not in (400, 404, 422, 503):
            return False
        if exc.status == 503 and not exc.no_endpoints:
            return False
        flat = exc.flat
        for name in _DROPPABLE_PARAMS:
            mentioned = name.replace("_", " ") in flat or (name == "max_tokens" and "max completion tokens" in flat)
            if name in payload and name not in tried and mentioned:
                tried.add(name)
                payload.pop(name)
                with _learned_lock:
                    _DROPPED_PARAMS.setdefault(model, set()).add(name)
                logger.warning("OpenRouter model %s rejected %s; dropping it from now on.", model, name)
                return True
        choice = payload.get("tool_choice")
        step = "named_tool_choice" if isinstance(choice, dict) else "forced_tool_choice"
        if "tool choice" in flat and choice not in (None, "auto") and step not in tried:
            tried.add(step)  # named -> "required" -> "auto": at most two steps
            with _learned_lock:
                if isinstance(choice, dict):
                    _NO_NAMED_TOOL_CHOICE.add(model)
                else:
                    _NO_FORCED_TOOL_CHOICE.add(model)
            payload["tool_choice"] = "required" if isinstance(choice, dict) else "auto"
            logger.warning(
                "OpenRouter model %s rejected tool_choice=%s; using %s.", model, choice, payload["tool_choice"]
            )
            return True
        if not exc.no_endpoints:
            return False
        sampling = [name for name in _SAMPLING_PARAMS if name in payload]
        if sampling and "sampling" not in tried:
            tried.add("sampling")
            for name in sampling:
                payload.pop(name)
            with _learned_lock:
                _DROPPED_PARAMS.setdefault(model, set()).update(sampling)
            logger.warning("OpenRouter found no endpoint for model %s with %s; dropping them.", model, sampling)
            return True
        routing = payload.get("provider")
        if isinstance(routing, dict) and routing.get("require_parameters") and "require_parameters" not in tried:
            tried.add("require_parameters")
            routing.pop("require_parameters")
            if not routing:
                payload.pop("provider")
            with _learned_lock:
                _NO_REQUIRE_PARAMETERS.add(model)
            logger.warning(
                "OpenRouter found no endpoint for model %s honouring every parameter; relaxing routing.", model
            )
            return True
        return False

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
        temperature: float | None = None,
    ) -> ConverseResult:
        model = self._model(model_id)
        payload = self._chat_payload(model, messages, system, tools, force_tool_use, temperature)
        tried: set[str] = set()
        while True:
            try:
                data = self._request("/chat/completions", payload)
            except _HttpError as exc:
                if self._apply_fallback(model, payload, exc, tried):
                    continue
                self._raise("chat completion", exc)
            return parse_chat_response(data)

    def converse_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Not a real stream (nothing in the app streams): one completion, replayed as ConverseStream-shaped
        events (`contentBlockDelta` with the text, then `messageStop`)."""
        result = self.converse(
            messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
        )
        if result.text:
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": result.text}}}
        yield {"messageStop": {"stopReason": result.stop_reason}}

    # --- embeddings ---------------------------------------------------------------------------------------------

    def embed(self, text: str, *, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
        model = self.embedding_model_id
        if "/" not in model:
            model = get_settings().openrouter_embed_model
        payload = {"model": model, "input": text, "dimensions": dimensions, "encoding_format": "float"}
        try:
            data = self._request("/embeddings", payload)
        except _HttpError as exc:
            self._raise("embeddings", exc)
        try:
            vector = [float(x) for x in data["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError):
            raise BedrockUnavailableError("OpenRouter embedding response had no usable 'embedding'.") from None
        if len(vector) != dimensions:
            raise BedrockUnavailableError(
                f"OpenRouter embedding model {model} returned {len(vector)} dimensions, expected {dimensions}."
            )
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0:
            raise BedrockUnavailableError("OpenRouter returned an all-zero embedding.")
        return [x / norm for x in vector]
