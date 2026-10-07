"""OpenRouter-backed implementation of `BedrockClientProtocol` (OpenAI chat-completions API).

Drop-in replacement for `BedrockClient`: the app still speaks the Bedrock-Converse-shaped
interface (`messages=[{"role", "content": [{"text": ...}]}]`, `ToolSpec`, `ConverseResult`),
and this module translates it to/from `POST {base_url}/chat/completions` and `/embeddings`.

Structured output is produced the same way as before: strict tool use (`tool_choice="required"`
when `force_tool_use`), falling back to `"auto"` when the model/provider rejects it (remembered
per model id in `_NO_FORCED_TOOL_CHOICE`). Errors are normalised to `BedrockUnavailableError` /
`BedrockTimeoutError` so every existing handler keeps working; a 429 message deliberately
contains "throttl" / "too many requests" because `pipeline/master._is_throttle` and the limiter
key off those words. The API key is NEVER logged or put in an exception message.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from typing import Any, NoReturn

import httpx

from app.ai.bedrock_client import (
    BedrockTimeoutError,
    BedrockUnavailableError,
    ConverseResult,
    ToolSpec,
    effective_max_output_tokens,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = 1024

# Models that rejected `tool_choice="required"`; only their first call pays for the failed try.
_NO_FORCED_TOOL_CHOICE: set[str] = set()

_FINISH_TO_STOP = {
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "content_filter": "content_filtered",
}

_MAX_RETRY_AFTER = 30.0


class _HttpError(Exception):
    """Internal: a non-2xx (or 200-with-`error`) response. Never escapes this module."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def _flatten_text(content: Any) -> str:
    """Bedrock content blocks `[{"text": ...}]` (or a plain string) -> one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _tool_to_openai(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _response_text(content: Any) -> str | None:
    """`message.content` may be a string or a list of parts (`{"type":"text","text":..}`)."""
    if content is None:
        return None
    text = _flatten_text(content)
    return text or None


def parse_chat_response(data: dict[str, Any]) -> ConverseResult:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise BedrockUnavailableError("OpenRouter response had no choices.")
    choice = choices[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason") or choice.get("native_finish_reason") or "unknown"
    stop_reason = _FINISH_TO_STOP.get(str(finish).lower(), str(finish))
    text = _response_text(message.get("content"))

    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    tool_calls = message.get("tool_calls") or []
    if tool_calls and isinstance(tool_calls[0], dict):
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
                # Malformed / cut-off JSON: report as truncated so callers retry smaller
                # instead of trusting a partial tool input.
                logger.warning("OpenRouter returned unparseable tool arguments (finish=%s).", finish)
                parsed = {}
                stop_reason = "max_tokens"
            tool_input = parsed if isinstance(parsed, dict) else {}
            if not isinstance(parsed, dict):
                stop_reason = "max_tokens"
        if stop_reason == "end_turn":
            stop_reason = "tool_use"
    return ConverseResult(
        stop_reason=stop_reason,
        text=text,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        raw=data,
    )


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), _MAX_RETRY_AFTER))
    except ValueError:
        return None


class OpenRouterClient:
    """`BedrockClientProtocol` over OpenRouter. Sync (httpx.Client with one shared pool);
    async call sites wrap calls in `asyncio.to_thread` exactly as they did for boto3."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        chat_model_id: str | None = None,
        judge_model_id: str | None = None,
        embedding_model_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.openrouter_api_key
        self._base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.chat_model_id = chat_model_id or settings.openrouter_chat_model_id
        self.judge_model_id = judge_model_id or settings.openrouter_judge_model_id
        self.embedding_model_id = embedding_model_id or settings.openrouter_embedding_model_id
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    # --- plumbing -----------------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    s = get_settings()
                    pool = max(s.bedrock_max_pool_connections, s.bedrock_max_concurrency * 2 + 8)
                    self._client = httpx.Client(
                        timeout=httpx.Timeout(
                            float(s.openrouter_read_timeout_seconds),
                            connect=float(s.openrouter_connect_timeout_seconds),
                        ),
                        limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool),
                        transport=self._transport,
                    )
        return self._client

    def _headers(self) -> dict[str, str]:
        s = get_settings()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if s.openrouter_http_referer:
            headers["HTTP-Referer"] = s.openrouter_http_referer
        if s.openrouter_app_title:
            headers["X-OpenRouter-Title"] = s.openrouter_app_title
            headers["X-Title"] = s.openrouter_app_title
        return headers

    def _scrub(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return text[:500]

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                msg = str(err.get("message") or "")
                raw = (err.get("metadata") or {}).get("raw") if isinstance(err.get("metadata"), dict) else None
                return f"{msg} {raw if isinstance(raw, str) else ''}".strip() or response.text[:300]
            if isinstance(err, str):
                return err
        except ValueError:
            pass
        return response.text[:300]

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with 502/503 retry+backoff. Returns the parsed body; raises `_HttpError` for
        any HTTP error or 200-with-`error` body, `BedrockTimeoutError` for transport timeouts."""
        if not self._api_key:
            raise BedrockUnavailableError("OpenRouter is not configured: OPENROUTER_API_KEY is empty.")
        attempts = max(2, get_settings().bedrock_max_attempts)
        url = f"{self._base_url}{path}"
        client = self._get_client()
        for attempt in range(attempts):
            try:
                response = client.post(url, json=payload, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise BedrockTimeoutError(f"OpenRouter {path} call timed out ({type(exc).__name__}).") from None
            except httpx.HTTPError as exc:
                raise BedrockUnavailableError(
                    f"OpenRouter {path} call failed: {type(exc).__name__}: {self._scrub(str(exc))}"
                ) from None
            if response.status_code in (502, 503) and attempt + 1 < attempts:
                delay = _parse_retry_after(response.headers.get("retry-after"))
                if delay is None:
                    delay = (2**attempt) + random.uniform(0, 0.5)
                # A 503 "no endpoints" (require_parameters) is a config rejection, not an outage.
                msg_l = self._error_message(response).lower()
                if "no endpoints" not in msg_l and "require_parameters" not in msg_l:
                    logger.warning("OpenRouter %s returned %s; retrying in %.1fs.", path, response.status_code, delay)
                    self._sleep(delay)
                    continue
            if response.status_code >= 400:
                raise _HttpError(
                    response.status_code,
                    self._error_message(response),
                    _parse_retry_after(response.headers.get("retry-after")),
                )
            try:
                data = response.json()
            except ValueError:
                raise BedrockUnavailableError("OpenRouter returned a non-JSON response.") from None
            if isinstance(data, dict) and data.get("error") and not data.get("choices") and not data.get("data"):
                err = data["error"]
                code = err.get("code") if isinstance(err, dict) else None
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise _HttpError(int(code) if isinstance(code, int | float) else 502, str(msg))
            if isinstance(data, dict):
                chs = data.get("choices")
                if chs and isinstance(chs[0], dict) and chs[0].get("error") and not chs[0].get("message"):
                    err = chs[0]["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise _HttpError(int(code) if isinstance(code, int | float) else 502, str(msg))
            return data
        raise BedrockUnavailableError(f"OpenRouter {path} call failed after retries.")  # pragma: no cover

    def _raise(self, operation: str, exc: _HttpError) -> NoReturn:
        status, detail = exc.status, self._scrub(exc.message)
        logger.error("OpenRouter %s failed with HTTP %s: %s", operation, status, detail)
        if status in (408, 504):
            raise BedrockTimeoutError(f"OpenRouter {operation} timed out (HTTP {status}): {detail}")
        if status == 429:
            raise BedrockUnavailableError(f"OpenRouter {operation} throttled: too many requests (HTTP 429): {detail}")
        if status == 402:
            raise BedrockUnavailableError(
                f"OpenRouter {operation} failed: insufficient credits (HTTP 402). Top up the account."
            )
        if status in (401, 403):
            raise BedrockUnavailableError(
                f"OpenRouter {operation} failed: authentication/permission error (HTTP {status}). "
                "Check OPENROUTER_API_KEY and model access."
            )
        raise BedrockUnavailableError(f"OpenRouter {operation} failed (HTTP {status}): {detail}")

    # --- Converse ------------------------------------------------------------------------

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
        settings = get_settings()
        model = model_id or self.chat_model_id
        out_messages: list[dict[str, Any]] = []
        if system:
            out_messages.append({"role": "system", "content": system})
        for m in messages:
            out_messages.append({"role": m.get("role", "user"), "content": _flatten_text(m.get("content"))})
        payload: dict[str, Any] = {"model": model, "messages": out_messages}
        if tools:
            payload["tools"] = [_tool_to_openai(t) for t in tools]
            if force_tool_use and model not in _NO_FORCED_TOOL_CHOICE:
                payload["tool_choice"] = "required"
                payload["parallel_tool_calls"] = False
            else:
                payload["tool_choice"] = "auto"
            payload["provider"] = {"require_parameters": True}
        max_tokens = effective_max_output_tokens(model)
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        effort = (settings.openrouter_reasoning_effort or "").strip()
        if effort:
            payload["reasoning"] = {"effort": effort}

        for _ in range(6):  # each fallback below can fire at most once
            try:
                return parse_chat_response(self._request("/chat/completions", payload))
            except _HttpError as exc:
                msg = exc.message.lower()
                flat = msg.replace("_", " ")
                if exc.status in (400, 404, 422, 503):
                    if "provider" in payload and ("no endpoints" in flat or "require parameters" in flat):
                        # Not every provider supports every parameter; drop the strict routing.
                        logger.warning("OpenRouter model %s: dropping require_parameters.", model)
                        payload.pop("provider")
                        continue
                    if payload.get("tool_choice") == "required" and "tool choice" in flat:
                        logger.warning("OpenRouter model %s rejected tool_choice=required.", model)
                        _NO_FORCED_TOOL_CHOICE.add(model)
                        payload["tool_choice"] = "auto"
                        payload.pop("parallel_tool_calls", None)
                        continue
                    if "max_tokens" in payload and ("max tokens" in flat or "max completion tokens" in flat):
                        logger.warning("OpenRouter model %s rejected max_tokens; retrying without.", model)
                        payload.pop("max_tokens")
                        continue
                    if "reasoning" in payload and "reasoning" in flat:
                        logger.warning("OpenRouter model %s rejected reasoning; retrying without.", model)
                        payload.pop("reasoning")
                        continue
                    if "parallel_tool_calls" in payload and "parallel" in flat:
                        payload.pop("parallel_tool_calls")
                        continue
                    if "provider" in payload and exc.status == 404:
                        payload.pop("provider")
                        continue
                self._raise("chat completion", exc)
        raise BedrockUnavailableError("OpenRouter chat completion failed after parameter fallbacks.")

    def converse_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ):
        raise NotImplementedError("Streaming is not implemented for the OpenRouter client.")

    # --- Embeddings ----------------------------------------------------------------------

    def embed(self, text: str, *, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
        payload = {
            "model": self.embedding_model_id,
            "input": text,
            "dimensions": dimensions,
            "encoding_format": "float",
        }
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
                f"OpenRouter embedding model {self.embedding_model_id} returned {len(vector)} "
                f"dimensions, expected {dimensions}."
            )
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0:
            raise BedrockUnavailableError("OpenRouter returned an all-zero embedding.")
        return [x / norm for x in vector]
