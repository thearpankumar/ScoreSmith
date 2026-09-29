"""Test doubles for `app.ai.bedrock_client.BedrockClientProtocol`.

This build environment has no real AWS Bedrock credentials (see the AI core's final
report / module docstrings), so every test that exercises AI-layer *logic* — the
LangGraph state machine, the judge's weighted-score math, the tool-call validation —
substitutes `FakeBedrockClient` for the real `BedrockClient` behind the shared Protocol,
per the task's explicit instruction to structure the Bedrock client behind an interface
that a fake can stand in for. No test here claims to have exercised a live Bedrock/GLM-5
call — the pgvector similarity tests use `find_similar_by_vector` directly with synthetic
vectors, entirely bypassing embedding generation.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any

from app.ai.bedrock_client import ConverseResult
from app.ai.web_search import SearchResult

ConverseFn = Callable[..., ConverseResult]
SearchFn = Callable[[str], "list[SearchResult]"]


class FakeBedrockClient:
    """Implements `BedrockClientProtocol` structurally (duck-typed, per the Protocol).

    Two scripting modes, combinable:
    - `script`: a plain FIFO queue of `ConverseResult`s, popped in call order. Fine for
      strictly sequential call patterns (the scorecard-builder graph makes one
      `.converse()` call per node visit).
    - `converse_fn`: a callable `(*, messages, system, tools, force_tool_use, model_id)
      -> ConverseResult` invoked instead of the queue when given. Used for the judge
      tests, where `asyncio.gather` fires several `.converse()` calls concurrently (via
      `asyncio.to_thread`) with no guaranteed ordering, so responses must be selected by
      inspecting the call's content instead of by position.
    """

    def __init__(
        self,
        script: list[ConverseResult] | None = None,
        converse_fn: ConverseFn | None = None,
        embedding_dim: int = 1024,
    ) -> None:
        self._script = list(script or [])
        self._converse_fn = converse_fn
        self.embedding_dim = embedding_dim
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[Any] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
    ) -> ConverseResult:
        # The lock only guards the shared `calls`/`script` list mutations, NOT the actual
        # `converse_fn` invocation. Concurrent callers (scorecard_builder.py's research
        # fan-out, judge.py's ensemble) each run `.converse()` inside `asyncio.to_thread`,
        # i.e. on real separate threads — holding the lock across `converse_fn` itself
        # would serialize those threads for the fn's entire duration (including any
        # deliberate `time.sleep`/barrier a concurrency-proof test uses inside it),
        # defeating the exact thing `converse_fn` mode exists to test. Only the bookkeeping
        # needs mutual exclusion.
        with self._lock:
            self.calls.append(
                {"messages": messages, "system": system, "tools": tools, "model_id": model_id}
            )
            converse_fn = self._converse_fn
            if converse_fn is None:
                if not self._script:
                    raise AssertionError("FakeBedrockClient.converse called with no scripted response left")
                return self._script.pop(0)
        return converse_fn(
            messages=messages,
            system=system,
            tools=tools,
            force_tool_use=force_tool_use,
            model_id=model_id,
        )

    def converse_stream(self, **kwargs: Any):  # pragma: no cover — not exercised in Cycle 1c
        raise NotImplementedError("FakeBedrockClient does not support streaming in tests.")

    def embed(self, text: str, *, dimensions: int = 1024) -> list[float]:
        """Deterministic pseudo-embedding: same idea as
        app/scripts/generate_scenarios.py's synthetic_embedding, kept local here so the
        AI-layer test suite has no import-time dependency on the seed script."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 for i in range(dimensions)]
        norm = sum(v * v for v in raw) ** 0.5 or 1.0
        return [v / norm for v in raw]


def tool_use_result(name: str, tool_input: dict[str, Any]) -> ConverseResult:
    return ConverseResult(stop_reason="tool_use", tool_name=name, tool_input=tool_input)


def text_result(text: str) -> ConverseResult:
    return ConverseResult(stop_reason="end_turn", text=text)


class FakeWebSearchClient:
    """Implements `app.ai.web_search.WebSearchClientProtocol` structurally (duck-typed).

    Two scripting modes, combinable — mirrors `FakeBedrockClient` above:
    - `script`: a plain FIFO queue of result-lists, popped in call order.
    - `search_fn`: a callable `(query: str) -> list[SearchResult]` invoked instead of the
      queue when given, e.g. to return different results depending on the query, or to
      raise (simulating what a real, exhausted-retries Gateway failure looks like from
      the caller's side — note the REAL client's own contract is that `.search()` never
      raises, only ever returns `[]` on failure; a `search_fn` that raises here is for
      testing that scorecard_builder.propose_kpis's own belt-and-suspenders try/except
      around the call handles a surprise exception gracefully too).

    `queries` records every query string passed to `.search()`, in call order, so tests
    can assert on the bounded-loop's exact search budget behaviour.
    """

    def __init__(
        self,
        script: list[list[SearchResult]] | None = None,
        search_fn: SearchFn | None = None,
    ) -> None:
        self._script = list(script or [])
        self._search_fn = search_fn
        self.queries: list[str] = []

    async def search(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        if self._search_fn is not None:
            return self._search_fn(query)
        if not self._script:
            return []
        return self._script.pop(0)


def search_result(title: str, url: str, snippet: str, published_date: str | None = None) -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, published_date=published_date)
