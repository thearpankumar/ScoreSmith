"""Thin wrapper around AWS Bedrock's `bedrock-runtime` client.

Model: **Z.ai GLM-5** via the global cross-region inference profile `global.zai.glm-5`
(see plan doc + `infra/.env.example`), used for both the chat scorecard builder and the
judge. GLM-5 does not support Bedrock's native structured-outputs feature
(`outputConfig.textFormat`, Claude-only), so structured output is produced via **strict
tool-use** instead: every call that needs a shaped response passes a `toolConfig` (a JSON
Schema tool definition) and forces the model to respond by calling that tool.

Two Bedrock APIs are used:
- **Converse / ConverseStream** for the chat model and the judge (GLM-5), since the
  Converse API's tool-use contract (`toolConfig` / `toolUse` content blocks) is a single,
  provider-agnostic shape shared across every Converse-compatible model on Bedrock — that
  is the whole point of the Converse API, so this wrapper implements against the
  documented Converse contract rather than anything model-specific. One assumption noted
  explicitly: GLM-5's exact support for `toolChoice: {"any": {}}` (force-a-tool-call) is
  unconfirmed in this build environment; `converse()` requests it and transparently
  retries without `toolChoice` (falling back to a strong system-prompt instruction) if
  Bedrock reports the field is unsupported for this model, so a caller never has to know
  which path was taken.
- **InvokeModel** for Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`), since
  Titan's embedding endpoint is not Converse-based.

Credentials/connectivity: this module never talks to AWS at import time — the boto3
client is created lazily on first use — and every call site here catches
credential/connectivity/throttling errors and re-raises a single `BedrockUnavailableError`
with a clear, logged message, rather than letting a raw botocore exception (or an import-
time crash) take down the whole app. This matters because this build environment almost
certainly has no real Bedrock credentials — see `backend/tests/` for how a
`FakeBedrockClient` substitutes for this class behind the shared `BedrockClientProtocol`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)


class BedrockUnavailableError(RuntimeError):
    """Raised whenever a Bedrock call cannot be completed — missing/invalid credentials,
    no network path to the Bedrock endpoint, throttling, or any other botocore-level
    failure. Callers should treat this as "the AI layer is temporarily unavailable", not
    as a validation error."""


@dataclass(frozen=True)
class ToolSpec:
    """One `toolConfig` tool definition, in Bedrock Converse's `toolSpec` shape."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_converse(self) -> dict[str, Any]:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": self.input_schema},
            }
        }


@dataclass
class ConverseResult:
    """Normalized result of a Converse call, regardless of whether the model replied with
    plain text or a tool call."""

    stop_reason: str
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tool_use(self) -> bool:
        return self.tool_name is not None


class BedrockClientProtocol(Protocol):
    """Interface both the real boto3-backed client and test fakes implement. Kept
    deliberately synchronous (matching boto3) — async call sites use
    `asyncio.to_thread(...)`."""

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ) -> ConverseResult: ...

    def converse_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ): ...

    def embed(self, text: str, *, dimensions: int = 1024) -> list[float]: ...


def _build_tool_config(tools: list[ToolSpec], force_tool_use: bool) -> dict[str, Any]:
    config: dict[str, Any] = {"tools": [t.to_converse() for t in tools]}
    if force_tool_use:
        # "any" = must call one of the provided tools (vs. "auto" = may reply in plain
        # text). This is the documented Converse toolChoice contract; see module
        # docstring for the fallback-without-toolChoice assumption.
        config["toolChoice"] = {"any": {}}
    return config


def _parse_converse_response(response: dict[str, Any]) -> ConverseResult:
    output_message = response.get("output", {}).get("message", {})
    content_blocks: list[dict[str, Any]] = output_message.get("content", [])
    stop_reason = response.get("stopReason", "unknown")

    text_parts: list[str] = []
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tool_use = block["toolUse"]
            tool_name = tool_use.get("name")
            tool_input = tool_use.get("input")
            tool_use_id = tool_use.get("toolUseId")

    return ConverseResult(
        stop_reason=stop_reason,
        text="\n".join(text_parts) if text_parts else None,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id=tool_use_id,
        raw=response,
    )


class BedrockClient:
    """Real boto3-backed implementation of `BedrockClientProtocol`."""

    def __init__(
        self,
        *,
        region: str | None = None,
        chat_model_id: str | None = None,
        judge_model_id: str | None = None,
        embedding_model_id: str | None = None,
    ) -> None:
        settings = get_settings()
        self._region = region or settings.aws_region
        self.chat_model_id = chat_model_id or settings.bedrock_chat_model_id
        self.judge_model_id = judge_model_id or settings.bedrock_judge_model_id
        self.embedding_model_id = embedding_model_id or settings.bedrock_embedding_model_id
        self._client: Any = None  # lazy — see module docstring

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # local import: keep boto3 off the app's import-time critical path

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ) -> ConverseResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "modelId": model_id or self.chat_model_id,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if tools:
            kwargs["toolConfig"] = _build_tool_config(tools, force_tool_use)

        try:
            response = client.converse(**kwargs)
        except Exception as exc:  # noqa: BLE001 — normalize every botocore failure mode
            if tools and force_tool_use and _looks_like_unsupported_tool_choice(exc):
                logger.warning(
                    "Bedrock model %s rejected toolChoice; retrying without it "
                    "(relying on system-prompt instruction instead).",
                    kwargs["modelId"],
                )
                kwargs["toolConfig"] = _build_tool_config(tools, force_tool_use=False)
                try:
                    response = client.converse(**kwargs)
                except Exception as retry_exc:  # noqa: BLE001
                    _raise_unavailable("Converse", retry_exc)
            else:
                _raise_unavailable("Converse", exc)
        return _parse_converse_response(response)

    def converse_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ):
        """Yields raw ConverseStream event dicts (`contentBlockDelta`, `messageStop`, ...).
        Provided for the frontend's future streaming chat UI; the LangGraph scorecard-
        builder node in `scorecard_builder.py` deliberately uses the non-streaming
        `converse()` above instead, since reconstructing tool-use JSON from streamed
        deltas adds complexity with no functional benefit for a request/response
        interrupt()-driven graph turn. Documented simplification, not an omission."""
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "modelId": model_id or self.chat_model_id,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if tools:
            kwargs["toolConfig"] = _build_tool_config(tools, force_tool_use)
        try:
            response = client.converse_stream(**kwargs)
            yield from response["stream"]
        except Exception as exc:  # noqa: BLE001
            _raise_unavailable("ConverseStream", exc)

    def embed(self, text: str, *, dimensions: int = 1024) -> list[float]:
        import json

        client = self._get_client()
        body = json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True})
        try:
            response = client.invoke_model(
                modelId=self.embedding_model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
        except Exception as exc:  # noqa: BLE001
            _raise_unavailable("InvokeModel (Titan embeddings)", exc)
        embedding = payload.get("embedding")
        if not embedding:
            raise BedrockUnavailableError(
                f"Titan embedding response had no 'embedding' field: {payload!r}"
            )
        return embedding


def _looks_like_unsupported_tool_choice(exc: Exception) -> bool:
    message = str(exc).lower()
    return "toolchoice" in message and ("not supported" in message or "unsupported" in message)


def _raise_unavailable(operation: str, exc: Exception) -> NoReturn:
    """Log the underlying botocore/credential error clearly, then raise the single
    normalized `BedrockUnavailableError` every call site above uses. Never lets a raw
    boto3/botocore exception (or an app crash) propagate past this module."""
    try:
        import botocore.exceptions as be

        if isinstance(exc, be.NoCredentialsError | be.PartialCredentialsError):
            logger.error(
                "Bedrock %s failed: no AWS credentials configured in this environment. "
                "This is expected in dev/build environments without real AWS access. (%s)",
                operation,
                exc,
            )
        elif isinstance(exc, be.EndpointConnectionError | be.ConnectTimeoutError):
            logger.error(
                "Bedrock %s failed: could not reach the Bedrock endpoint (network/DNS "
                "issue or no outbound access from this environment). (%s)",
                operation,
                exc,
            )
        elif isinstance(exc, be.ClientError):
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            logger.error("Bedrock %s failed with ClientError %s: %s", operation, code, exc)
        else:
            logger.error("Bedrock %s failed: %s: %s", operation, type(exc).__name__, exc)
    except ImportError:
        logger.error("Bedrock %s failed: %s: %s", operation, type(exc).__name__, exc)

    raise BedrockUnavailableError(f"Bedrock {operation} call failed: {exc}") from exc
