"""Client for TypeSafe AI's **Jev** ("System One" decision model), accessed via
OpenRouter — used exclusively for the three quality-gate checkpoints added to
`scorecard_builder.py` (KPI-category planning, each category research agent's finding, and
the final per-turn answer). Every normal chat/judge LLM call in this project stays on AWS
Bedrock (GLM-5/GLM-4.7-Flash — see `bedrock_client.py`); this module is a completely
separate, additive dependency.

**Research findings this client is built from** (a dedicated research subagent was
spawned to establish these — this is a real, fairly new/niche product, not something to
guess at from general training knowledge):

- Jev has **no text-generation surface at all** ("no text generation" was already a known
  failure mode going into this feature — confirmed here to mean literally no
  `/chat/completions`-style endpoint exists for it). It is reached through a **separate,
  alpha-status "Decisions API"**: `POST https://openrouter.ai/api/alpha/decisions`, not
  OpenRouter's standard `/api/v1/chat/completions` endpoint. It does not appear in
  OpenRouter's general `/api/v1/models` listing either — it is a distinct product
  surface, not a normal chat model.
- Model identifier used here: the **pinned** slug `typesafe/jev-1.13` (not the rolling
  `~typesafe/jev-latest` alias — a pinned version is more predictable for a quality
  threshold this project hardcodes at 0.75).
- Auth: a normal OpenRouter API key (`OPENROUTER_JEV_API` — read via
  `Settings.openrouter_jev_api`) as a Bearer token. Same OpenRouter account/billing as any
  other OpenRouter usage; no separate TypeSafe account needed for this route.
- Request shape is NOT a prompt — it's a `state` (arbitrary JSON, the "input") plus one or
  more `questions`, each with a `type` (`"noul"` = a single yes/no proposition returning a
  continuous 0-1 probability; `"choice"` = pick 1 of N labeled options; `"score"` = an
  ordered rubric). For this project's "0-1 instruction/answer match score" use case, a
  single `noul` question ("does the answer satisfy the instruction?") is the correct
  primitive — its `noul` field IS the 0-1 score directly, no parsing of generated text
  required (there is none to parse).
- Response shape: `{"answers": {"<question_key>": {"type": "noul", "noul": <float 0-1>}},
  "usage": {...}, "id": ..., "model": ..., "provider": "TypeSafe"}`.
- **Live-confirmed** (this build, 2026-09-30, real OpenRouter key from `infra/.env`): a
  genuinely relevant (instruction, answer) pair scored `noul=0.92`; a deliberately
  unrelated answer to the same instruction scored `noul=0.01` — proving real
  discrimination, not a constant/stubbed response. See this task's final report for the
  raw request/response pairs.

**Graceful degradation (deliberate, not accidental)**: `quality_gate()` below is the ONE
chokepoint every call site in `scorecard_builder.py` goes through. It NEVER raises — a
missing `OPENROUTER_JEV_API` key, a network failure, a non-2xx response, or a malformed
response body are all treated identically: log a warning and report the gate as
**PASSED** (`degraded=True`, `score=None`). A third-party dependency being unreachable
must never block or crash the core chat pipeline; Jev is an additive self-critique layer,
not a hard dependency the product breaks without.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
# Jev's own documented latency is 70-500ms (non-autoregressive single pass); this leaves
# generous headroom for OpenRouter/network overhead on top of that without letting one
# slow call stall a chat turn indefinitely.
_TIMEOUT_SECONDS = 15.0

QUALITY_GATE_THRESHOLD = 0.75

_SATISFIES_QUESTION: dict[str, Any] = {
    "satisfies": {
        "type": "noul",
        "instructions": "Does the answer fully and correctly satisfy the instruction?",
        "criteria": {
            "true": "The answer directly and correctly addresses everything the instruction asks for.",
            "false": "The answer is incomplete, incorrect, or does not address the instruction.",
        },
    }
}


class JevUnavailableError(RuntimeError):
    """Raised by `JevClient.rate_match` on any failure — missing API key, network/HTTP
    failure, or a response with no usable `noul` score. Mirrors
    `BedrockUnavailableError`'s/`WebSearchClientProtocol`'s own "normalize every failure
    mode into one exception type" pattern. Callers wanting graceful-degradation "gate"
    semantics should go through `quality_gate()` below rather than catching this
    directly."""


@dataclass(frozen=True)
class JevRatingResult:
    """One successful Jev rating — `score` is always clamped to [0, 1]."""

    score: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JevChoiceResult:
    """One successful Jev `choice` answer: the picked option key, the per-option probability
    distribution and Jev's own 0-1 confidence (how concentrated that distribution is)."""

    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


class JevClientProtocol(Protocol):
    """Interface both the real OpenRouter-backed client and `tests/fakes.py::FakeJevClient`
    implement — mirrors `BedrockClientProtocol`/`WebSearchClientProtocol`'s own pattern
    (see their module docstrings) so every quality-gate call site in
    `scorecard_builder.py` is fakeable/deterministic in tests. Async (like
    `WebSearchClientProtocol`, unlike the boto3-wrapping `BedrockClientProtocol`) since
    this is built directly on `httpx.AsyncClient` with no synchronous SDK underneath."""

    async def rate_match(self, *, instruction: str, answer: str) -> JevRatingResult: ...

    async def choose(
        self, *, state: dict[str, Any], instructions: str, options: dict[str, str]
    ) -> JevChoiceResult: ...


class JevClient:
    """Real implementation of `JevClientProtocol`, backed by OpenRouter's alpha Decisions
    API (see module docstring). Never talks to OpenRouter at construction time — nothing
    is created until the first real `rate_match()` call, mirroring
    `AgentCoreWebSearchClient`'s own lazy-construction pattern."""

    def __init__(self, *, api_key: str | None = None, model_id: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.openrouter_jev_api
        self._model_id = model_id if model_id is not None else settings.openrouter_jev_model_id

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def rate_match(self, *, instruction: str, answer: str) -> JevRatingResult:
        if not self.is_configured:
            raise JevUnavailableError(
                "OPENROUTER_JEV_API is not configured; cannot call the Jev quality gate."
            )

        body = {
            "model": self._model_id,
            "state": {"instruction": instruction, "answer": answer},
            "questions": _SATISFIES_QUESTION,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    _DECISIONS_URL,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001 — normalize every httpx/JSON failure mode
            raise JevUnavailableError(f"Jev decisions call failed: {exc}") from exc

        try:
            raw_score = data["answers"]["satisfies"]["noul"]
            score = float(raw_score)
        except (KeyError, TypeError, ValueError) as exc:
            raise JevUnavailableError(
                f"Jev decisions response had no usable 'satisfies.noul' score: {data!r}"
            ) from exc

        return JevRatingResult(score=max(0.0, min(1.0, score)), raw=data)

    async def choose(
        self, *, state: dict[str, Any], instructions: str, options: dict[str, str]
    ) -> JevChoiceResult:
        """Jev's native `choice` primitive ("pick 1 of N labeled options" — see the module
        docstring; per OpenRouter's Jev docs the answer is `{"type": "choice", "choice": key,
        "confidence": 0-1, "probabilities": {key: p}}`). Used as the cheap intent router in
        `request_routing.route_request`. Raises `JevUnavailableError` on any failure or if the
        returned choice is not one of `options` (never trust a label we did not offer)."""
        if not self.is_configured:
            raise JevUnavailableError("OPENROUTER_JEV_API is not configured; cannot call Jev choice.")
        body = {
            "model": self._model_id,
            "state": state,
            "questions": {"pick": {"type": "choice", "instructions": instructions, "criteria": options}},
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    _DECISIONS_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
            answer = data["answers"]["pick"]
            choice = str(answer["choice"])
            confidence = float(answer.get("confidence", 0.0))
            probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
        except Exception as exc:  # noqa: BLE001 — normalize every httpx/JSON/shape failure mode
            raise JevUnavailableError(f"Jev choice call failed: {exc}") from exc
        if choice not in options:
            raise JevUnavailableError(f"Jev returned an unknown option {choice!r}.")
        return JevChoiceResult(choice=choice, confidence=max(0.0, min(1.0, confidence)), probabilities=probabilities)


# --- Graceful-degradation gate helper (the one chokepoint scorecard_builder.py uses) -----


@dataclass(frozen=True)
class QualityGateResult:
    """Result of one `quality_gate()` call — ALWAYS has a definite `passed` verdict, never
    raises. `degraded=True` means Jev could not be reached/parsed (see
    `JevUnavailableError`) and the gate was treated as **PASSED** by deliberate policy
    (see module docstring's "graceful degradation" section) — `score` is `None` only in
    this case, since no real rating was obtained."""

    passed: bool
    score: float | None
    degraded: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


async def quality_gate(
    client: JevClientProtocol | None,
    *,
    instruction: str,
    answer: str,
    threshold: float = QUALITY_GATE_THRESHOLD,
) -> QualityGateResult:
    """Rates `(instruction, answer)` via `client.rate_match(...)` and compares the
    resulting score against `threshold`. NEVER raises — every `scorecard_builder.py`
    checkpoint goes through this single function rather than calling `rate_match`
    directly, so the graceful-degradation policy lives in exactly one place.

    `client is None` (Jev not wired into this call site at all — e.g. a unit test not
    exercising the quality gate, or a deployment that never configured
    `OPENROUTER_JEV_API`) degrades identically to a live Jev failure: gate reported as
    passed, logged, `degraded=True`."""
    if client is None:
        logger.info("quality_gate: no Jev client configured for this call; treating gate as passed.")
        return QualityGateResult(passed=True, score=None, degraded=True)
    try:
        # Hard per-call ceiling: httpx's timeout is per phase (connect/write/read each), so a
        # slow endpoint can still take far longer than `_TIMEOUT_SECONDS` end to end (observed
        # 5-80s per call live). A call over the ceiling is treated exactly like an unreachable Jev.
        result = await asyncio.wait_for(
            client.rate_match(instruction=instruction, answer=answer),
            timeout=get_settings().quality_gate_call_timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — see module docstring: a Jev failure must never crash a turn
        logger.warning(
            "quality_gate: Jev call failed or timed out; treating gate as passed (graceful degradation).",
            exc_info=True,
        )
        return QualityGateResult(passed=True, score=None, degraded=True)
    return QualityGateResult(passed=result.score >= threshold, score=result.score, raw=result.raw)
