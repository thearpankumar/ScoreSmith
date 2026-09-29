"""Client for AWS AgentCore Gateway's web-search MCP tool.

Gives the chat-driven scorecard builder (`app/ai/scorecard_builder.py`'s `propose_kpis`
node) a way to research the *real* web for industry KPIs/benchmarks/thresholds for a
domain the user describes, rather than proposing KPIs purely from the model's training
data.

**Transport**: a plain signed HTTP POST, not boto3 — `bedrock-agentcore` (the AgentCore
Gateway) has no botocore service model, so this hand-rolls AWS Signature Version 4 over
`httpx` instead. Only the `host` and `x-amz-date` headers are signed (`signed_headers`),
using the standard AWS4-HMAC-SHA256 derivation chain: `"AWS4" + secret` -> date key ->
region key -> service key -> `aws4_request` key -> signature. The service string is
literally `"bedrock-agentcore"`; region comes from `Settings.aws_region` (the same region
the main Bedrock client uses); credentials are the dedicated
`AGENTCORE_GATEWAY_AWS_ACCESS_KEY_ID`/`AGENTCORE_GATEWAY_AWS_SECRET_ACCESS_KEY` pair — a
separate IAM principal from whatever credentials boto3's default chain resolves for
`bedrock_client.py`.

**Request shape**: a JSON-RPC 2.0 / MCP `tools/call` envelope:
    {"jsonrpc": "2.0", "method": "tools/call",
     "params": {"name": <tool name>, "arguments": {"query": <query>}}, "id": 1}

**Response shape**: `data["result"]["content"]` is a list; each item's `"text"` field is
itself a JSON string (a second `json.loads` is required) shaped
`{"id": ..., "results": [{"text", "url", "title", "publishedDate"}, ...]}`. Parsing here
is defensive about the inner value being a dict-with-`results`, a bare list, or a single
dict, since this is the first time this project has talked to a live Gateway and the
exact response shape has only been verified against a sibling project's implementation,
not this one's own live traffic yet.

**Failure handling**: on any error (HTTP error, timeout, malformed response, ...), retry
with exponential backoff + jitter (`2**attempt + random jitter`, capped at 3 attempts);
if still failing, return an **empty list** rather than raising. A failed web search must
never kill the whole chat turn — the calling node falls back to proposing KPIs from the
model's own knowledge, same as before this feature existed.

**Concurrency guard**: a simple in-process `asyncio.Semaphore` (no Redis / cluster-wide
rate limiting — disproportionate for this project's scale and this project's Docker
Compose has no Redis service).

Structured behind `WebSearchClientProtocol`, mirroring exactly how
`bedrock_client.py`'s `BedrockClientProtocol` is structured, so `AgentCoreWebSearchClient`
can be swapped for `tests/fakes.py::FakeWebSearchClient` in tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_SERVICE = "bedrock-agentcore"
_ALGORITHM = "AWS4-HMAC-SHA256"
_MAX_RESULTS = 10
_MAX_ATTEMPTS = 3
_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_CONCURRENCY = 4


@dataclass(frozen=True)
class SearchResult:
    """One normalized web search result."""

    title: str
    url: str
    snippet: str
    published_date: str | None = None


class WebSearchClientProtocol(Protocol):
    """Interface both the real Gateway-backed client and test fakes implement. Async
    (unlike `BedrockClientProtocol`, which wraps synchronous boto3) since this client is
    built directly on `httpx.AsyncClient` with no synchronous SDK underneath."""

    async def search(self, query: str) -> list[SearchResult]: ...


# --- AWS SigV4 signing (hand-rolled — see module docstring for why no boto3) -------------


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _derive_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def _sigv4_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    region: str,
    access_key: str,
    secret_key: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Builds `Authorization` + `x-amz-date` + `host` headers for a SigV4-signed request,
    signing only those two headers (`host`, `x-amz-date`) per the reference
    implementation this pattern was copied from. Merged by the caller onto
    `Content-Type: application/json`."""
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"
    moment = now or datetime.now(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")

    canonical_headers = f"host:{host}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-date"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )

    credential_scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = _derive_signing_key(secret_key, date_stamp, region, _SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{_ALGORITHM} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    return {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "host": host,
        "Content-Type": "application/json",
    }


# --- Response parsing ---------------------------------------------------------------------


def _normalize_result(raw: dict[str, Any]) -> SearchResult:
    title = raw.get("title") or ""
    url = raw.get("url") or ""
    snippet = raw.get("text") or raw.get("snippet") or raw.get("content") or ""
    published_date = raw.get("publishedDate") or raw.get("published_date")
    return SearchResult(
        title=str(title)[:200],
        url=str(url)[:300],
        snippet=str(snippet)[:500],
        published_date=str(published_date) if published_date else None,
    )


def _coerce_raw_results(inner: Any) -> list[dict[str, Any]]:
    """`inner` (the second, nested `json.loads`d payload) has been observed shaped as a
    dict with a `results` list, but is handled defensively as a bare list or a single
    result dict too — see module docstring."""
    if isinstance(inner, dict) and isinstance(inner.get("results"), list):
        return [r for r in inner["results"] if isinstance(r, dict)]
    if isinstance(inner, list):
        return [r for r in inner if isinstance(r, dict)]
    if isinstance(inner, dict):
        return [inner]
    return []


def extract_results(data: dict[str, Any]) -> list[SearchResult]:
    """Parses a full MCP `tools/call` JSON-RPC response body into normalized
    `SearchResult`s, capped at `_MAX_RESULTS`. Never raises on a malformed/unexpected
    shape — returns whatever it could parse (possibly empty)."""
    results: list[SearchResult] = []
    try:
        content = ((data.get("result") or {}).get("content")) or []
    except AttributeError:
        return results
    if not isinstance(content, list):
        return results

    for item in content:
        if len(results) >= _MAX_RESULTS:
            break
        text = item.get("text") if isinstance(item, dict) else None
        if not text or not isinstance(text, str):
            continue
        try:
            inner = json.loads(text)
        except json.JSONDecodeError:
            continue
        for raw in _coerce_raw_results(inner):
            results.append(_normalize_result(raw))
            if len(results) >= _MAX_RESULTS:
                break
    return results


# --- Real client ---------------------------------------------------------------------------


class AgentCoreWebSearchClient:
    """Real implementation of `WebSearchClientProtocol`, backed by AWS AgentCore
    Gateway's web-search MCP tool. Never talks to the Gateway at construction time —
    everything is resolved lazily inside `search()` — and every failure mode (missing
    config, HTTP error, timeout, malformed response) is caught here rather than allowed
    to propagate, since `search()`'s contract is "never raises, worst case returns []"
    (see module docstring)."""

    def __init__(
        self,
        *,
        url: str | None = None,
        tool_name: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        settings = get_settings()
        self._url = url if url is not None else settings.agentcore_gateway_web_search_url
        self._tool_name = (
            tool_name if tool_name is not None else settings.agentcore_gateway_web_search_tool_name
        )
        self._access_key = (
            access_key if access_key is not None else settings.agentcore_gateway_aws_access_key_id
        )
        self._secret_key = (
            secret_key if secret_key is not None else settings.agentcore_gateway_aws_secret_access_key
        )
        self._region = region or settings.aws_region
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @property
    def is_configured(self) -> bool:
        return bool(self._url and self._tool_name and self._access_key and self._secret_key)

    async def search(self, query: str) -> list[SearchResult]:
        if not self.is_configured:
            logger.warning(
                "AgentCore Gateway web search is not configured (missing one of "
                "AGENTCORE_GATEWAY_WEB_SEARCH_URL/_TOOL_NAME/_AWS_ACCESS_KEY_ID/"
                "_AWS_SECRET_ACCESS_KEY); returning no results for query=%r.",
                query,
            )
            return []

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": self._tool_name, "arguments": {"query": query}},
                "id": 1,
            }
        ).encode("utf-8")

        async with self._semaphore:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    headers = _sigv4_headers(
                        method="POST",
                        url=self._url,
                        body=body,
                        region=self._region,
                        access_key=self._access_key,
                        secret_key=self._secret_key,
                    )
                    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                        response = await client.post(self._url, content=body, headers=headers)
                        response.raise_for_status()
                        data = response.json()
                    results = extract_results(data)
                    logger.info(
                        "web_search query=%r -> %d result(s) (attempt %d/%d)",
                        query,
                        len(results),
                        attempt + 1,
                        _MAX_ATTEMPTS,
                    )
                    return results
                except Exception:  # noqa: BLE001 — see class docstring: never raise from search()
                    is_last = attempt + 1 >= _MAX_ATTEMPTS
                    logger.warning(
                        "web_search query=%r failed on attempt %d/%d%s",
                        query,
                        attempt + 1,
                        _MAX_ATTEMPTS,
                        " — giving up, returning []" if is_last else " — retrying",
                        exc_info=True,
                    )
                    if is_last:
                        return []
                    backoff = (2**attempt) + random.uniform(0, 1)
                    await asyncio.sleep(backoff)
        return []  # pragma: no cover — unreachable (loop always returns/retries)
