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
from app.ai.jev_client import JevChoiceResult, JevRatingResult
from app.ai.web_search import SearchResult

ConverseFn = Callable[..., ConverseResult]
SearchFn = Callable[[str], "list[SearchResult]"]
RateFn = Callable[..., float]


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

    The request-classification call (`classify_request` — see `research_kpis` in
    scorecard_builder.py) is handled OUT OF BAND: it is answered by `classify_fn` (default:
    "open_ended", i.e. the pre-existing behaviour) and logged to `classify_calls`, never to
    `calls` and never consuming `script`/`converse_fn`. That keeps every test written
    before the classifier existed (FIFO scripts, exact `len(calls)` assertions) valid
    unchanged, while tests of the user-specified mode script `classify_fn` explicitly.
    """

    def __init__(
        self,
        script: list[ConverseResult] | None = None,
        converse_fn: ConverseFn | None = None,
        embedding_dim: int = 1024,
        classify_fn: ConverseFn | None = None,
        route_fn: ConverseFn | None = None,
        dedup_fn: ConverseFn | None = None,
        header_fn: ConverseFn | None = None,
        side_fn: ConverseFn | None = None,
    ) -> None:
        self._side_fn = side_fn
        self.side_calls: list[dict[str, Any]] = []
        self._header_fn = header_fn
        self.header_calls: list[dict[str, Any]] = []
        self._script = list(script or [])
        self._converse_fn = converse_fn
        self._classify_fn = classify_fn
        self._route_fn = route_fn
        self._dedup_fn = dedup_fn
        self.dedup_calls: list[dict[str, Any]] = []
        self.route_calls: list[dict[str, Any]] = []
        self.embedding_dim = embedding_dim
        self.calls: list[dict[str, Any]] = []
        self.classify_calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[Any] | None = None,
        force_tool_use: bool = False,
        model_id: str | None = None,
        temperature: float | None = None,
    ) -> ConverseResult:
        # The lock only guards the shared `calls`/`script` list mutations, NOT the actual
        # `converse_fn` invocation. Concurrent callers (scorecard_builder.py's research
        # fan-out, judge.py's ensemble) each run `.converse()` inside `asyncio.to_thread`,
        # i.e. on real separate threads — holding the lock across `converse_fn` itself
        # would serialize those threads for the fn's entire duration (including any
        # deliberate `time.sleep`/barrier a concurrency-proof test uses inside it),
        # defeating the exact thing `converse_fn` mode exists to test. Only the bookkeeping
        # needs mutual exclusion.
        if tools and any(getattr(t, "name", None) == "judge_duplicate_kpis" for t in tools):
            # The borderline duplicate-KPI judge (scorecard_builder._judge_duplicate_pairs) — out of
            # band like the router. Default: nothing is a duplicate (keep both).
            with self._lock:
                self.dedup_calls.append({"messages": messages, "system": system, "model_id": model_id})
            if self._dedup_fn is not None:
                return self._dedup_fn(
                    messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
                )
            return tool_use_result("judge_duplicate_kpis", {"same_concept_ids": []})
        if tools and any(getattr(t, "name", None) == "set_scorecard_header" for t in tools):
            # The early scorecard-header call (scorecard_builder._fill_header) — out of band. Default:
            # an empty answer, so the deterministic derivation fills the header.
            with self._lock:
                self.header_calls.append({"messages": messages, "system": system, "model_id": model_id})
            if self._header_fn is not None:
                return self._header_fn(
                    messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
                )
            return tool_use_result("set_scorecard_header", {})
        if tools and any(getattr(t, "name", None) == "answer_user_questions" for t in tools):
            # The side-question answer call (scorecard_builder._answer_side_questions) — out of band;
            # default: no answer, so tests written before it existed are unaffected.
            with self._lock:
                self.side_calls.append({"messages": messages, "system": system})
            if self._side_fn is not None:
                return self._side_fn(
                    messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
                )
            return tool_use_result("answer_user_questions", {"answer": ""})
        if tools and any(getattr(t, "name", None) == "route_request" for t in tools):
            # The cheap small-model intent router (request_routing.route_request) — also out of
            # band. Default "unsure" makes every pre-router test fall through to the main-model
            # classification exactly as before.
            with self._lock:
                self.route_calls.append({"messages": messages, "system": system, "tools": tools, "model_id": model_id})
            if self._route_fn is not None:
                return self._route_fn(
                    messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
                )
            return tool_use_result("route_request", {"mode": "unsure"})
        if tools and any(getattr(t, "name", None) == "classify_request" for t in tools):
            with self._lock:
                self.classify_calls.append({"messages": messages, "system": system, "tools": tools})
            if self._classify_fn is not None:
                return self._classify_fn(
                    messages=messages, system=system, tools=tools, force_tool_use=force_tool_use, model_id=model_id
                )
            return tool_use_result(
                "classify_request", {"mode": "open_ended", "reasoning": "default fake classification", "kpis": []}
            )
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


class FakeJevClient:
    """Implements `app.ai.jev_client.JevClientProtocol` structurally (duck-typed) — same
    spirit/scripting shape as `FakeBedrockClient`/`FakeWebSearchClient` above.

    Two scripting modes, combinable:
    - `script`: a plain FIFO queue of 0-1 floats, popped in call order.
    - `rate_fn`: a callable `(*, instruction: str, answer: str) -> float` invoked instead
      of the queue when given — lets a test vary the score by WHICH checkpoint/content is
      being rated (e.g. always score a specific angle's finding low), or raise to simulate
      a genuine Jev/OpenRouter failure (the real client's contract is that `rate_match`
      raises `JevUnavailableError` on failure — `quality_gate()` is what catches that and
      degrades to "passed"; a `rate_fn` that raises here exercises that exact path).

    `calls` records every `(instruction, answer)` pair passed to `.rate_match()`, in call
    order, so tests can assert on exactly what was rated at each checkpoint.
    """

    def __init__(
        self,
        script: list[float] | None = None,
        rate_fn: RateFn | None = None,
        choose_fn: Callable[..., JevChoiceResult] | None = None,
    ) -> None:
        self._script = list(script or [])
        self._rate_fn = rate_fn
        self._choose_fn = choose_fn
        self.calls: list[dict[str, str]] = []
        self.choose_calls: list[dict[str, Any]] = []

    async def choose(self, *, state: dict[str, Any], instructions: str, options: dict[str, str]) -> JevChoiceResult:
        """`choose_fn` scripts the intent-router answer; by default this fake is "unsure"
        (a low-confidence open_ended pick) so tests written before the router existed fall
        through to the unchanged main-model classification."""
        self.choose_calls.append({"state": state, "instructions": instructions, "options": options})
        if self._choose_fn is not None:
            return self._choose_fn(state=state, instructions=instructions, options=options)
        return JevChoiceResult(choice="open_ended", confidence=0.0)

    async def rate_match(self, *, instruction: str, answer: str) -> JevRatingResult:
        self.calls.append({"instruction": instruction, "answer": answer})
        if self._rate_fn is not None:
            score = self._rate_fn(instruction=instruction, answer=answer)
        elif self._script:
            score = self._script.pop(0)
        else:
            score = 1.0
        return JevRatingResult(score=score)


def full_rubric(text: str = "Level") -> dict[str, dict[str, Any]]:
    """A complete, distinct 11-level (0-10) guideline set — a leaf KPI is only complete (confirmable,
    materializable) with every level present."""
    return {str(i): {"qualitative_text": f"{text} {i}", "quantitative_criteria": None} for i in range(11)}


# --- AI evaluation pipeline fakes ---------------------------------------------------------------------

DEFAULT_QUOTE = "Our solution reduces fleet downtime by 30 percent"


def default_corpus(evaluation_id: str, quote: str = DEFAULT_QUOTE) -> dict[str, Any]:
    return {
        "evaluation_id": evaluation_id,
        "built_at": "2026-10-04T00:00:00Z",
        "stats": {"docs": 1, "videos": 0, "images": 0, "words": 40},
        "warnings": [],
        "sections": [
            {
                "id": "s001",
                "source_id": "src1",
                "source_name": "report.pdf",
                "kind": "doc",
                "label": "DOC report.pdf p1",
                "text": f"FleetPulse by Jane Doe (jane@example.com). {quote}. It uses telemetry and ML models.",
            },
            {
                "id": "s002",
                "source_id": "src1",
                "source_name": "report.pdf",
                "kind": "doc",
                "label": "DOC report.pdf p2",
                "text": "The architecture has an ingestion layer, a feature store and a dashboard for dispatchers.",
            },
        ],
    }


class FakeAwsJobs:
    """In-memory `AwsJobsProtocol`. Executions stay RUNNING while `hold` is true (finish them with
    `finish(evaluation_id, ...)`); otherwise they end immediately with `default_outcome`."""

    def __init__(self, *, hold: bool = False, default_outcome: str = "SUCCEEDED") -> None:
        self.hold = hold
        self.default_outcome = default_outcome
        self.objects: dict[str, bytes] = {}
        self.json_objects: dict[str, dict[str, Any]] = {}
        self.multiparts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.aborted: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.executions: dict[str, dict[str, Any]] = {}
        self.start_names: list[str] = []
        self.start_payloads: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.corpus_text: str | None = None
        self.fail_start: Exception | None = None
        self._lock = threading.Lock()

    # --- S3 ---
    def create_multipart(self, key: str, content_type: str | None) -> str:
        upload_id = f"up-{len(self.multiparts) + 1}"
        self.multiparts[(key, upload_id)] = []
        return upload_id

    def presign_part(self, key: str, upload_id: str, part_number: int, expires: int) -> str:
        return f"https://s3.fake/{key}?uploadId={upload_id}&partNumber={part_number}&expires={expires}"

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
        if (key, upload_id) not in self.multiparts:
            from app.pipeline.aws_jobs import AwsJobsError

            raise AwsJobsError("NoSuchUpload")
        self.multiparts[(key, upload_id)] = parts
        self.objects.setdefault(key, b"")

    def abort_multipart(self, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))
        self.multiparts.pop((key, upload_id), None)

    def put_object(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def head_object(self, key: str):
        from app.pipeline.aws_jobs import ObjectHead

        if key not in self.objects:
            return None
        return ObjectHead(size=len(self.objects[key]))

    def read_range(self, key: str, start: int, length: int) -> bytes:
        return self.objects[key][start : start + length]

    def get_object_bytes(self, key: str, max_bytes: int) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def read_json(self, key: str) -> dict[str, Any] | None:
        return self.json_objects.get(key)

    # --- Step Functions ---
    def start_execution(self, name: str, payload: dict[str, Any]) -> str:
        if self.fail_start is not None:
            raise self.fail_start
        with self._lock:
            self.start_names.append(name)
            self.start_payloads.append(payload)
            arn = f"arn:aws:states:us-east-1:123:execution:qs-eval-pipeline:{name}"
            self.executions[arn] = {"status": "RUNNING", "payload": payload, "name": name}
        if not self.hold:
            self.finish(payload["evaluation_id"], self.default_outcome)
        return arn

    def describe_execution(self, arn: str):
        from app.pipeline.aws_jobs import ExecutionInfo

        ex = self.executions[arn]
        return ExecutionInfo(status=ex["status"], error=ex.get("error"), cause=ex.get("cause"))

    def stop_execution(self, arn: str, cause: str) -> None:
        self.stopped.append(arn)
        if arn in self.executions:
            self.executions[arn]["status"] = "ABORTED"

    # --- test controls ---
    def finish(
        self,
        evaluation_id: str,
        outcome: str = "SUCCEEDED",
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        corpus: dict[str, Any] | None = None,
    ) -> None:
        arn = next(
            (a for a, e in reversed(list(self.executions.items())) if e["payload"]["evaluation_id"] == evaluation_id),
            None,
        )
        assert arn is not None, f"no execution for {evaluation_id}"
        base = f"derived/{evaluation_id}"
        if outcome == "SUCCEEDED":
            if corpus is not None:
                self.json_objects[f"{base}/corpus.json"] = corpus
            elif f"{base}/corpus.json" not in self.json_objects:
                text = self.corpus_text or DEFAULT_QUOTE
                self.json_objects[f"{base}/corpus.json"] = default_corpus(evaluation_id, text)
        elif error_code:
            self.json_objects[f"{base}/status.json"] = {
                "error_code": error_code,
                "error_message": error_message or error_code,
            }
        self.executions[arn]["status"] = outcome

    def set_progress(self, evaluation_id: str, progress: dict[str, Any]) -> None:
        self.json_objects[f"derived/{evaluation_id}/progress.json"] = progress


class FakeJevScoreClient:
    """Implements `JevScoreClientProtocol`. `position` is the 0-indexed criteria position returned."""

    def __init__(self, position: float = 6.0, noul: float | None = 0.9, fail: bool = False) -> None:
        self.position = position
        self.noul = noul
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def score(
        self, *, state: dict[str, Any], instructions: str, criteria: list[str], relevance_instructions: str
    ):
        from app.ai.jev_client import JevUnavailableError
        from app.pipeline.jev_scorer import JevScoreAnswer

        self.calls.append({"state": state, "criteria": criteria})
        if self.fail:
            raise JevUnavailableError("jev down")
        n = len(criteria)
        probs = [0.0] * n
        probs[min(n - 1, int(round(self.position)))] = 1.0
        return JevScoreAnswer(position=self.position, probabilities=probs, confidence=0.8, noul=self.noul)


def master_converse_fn(quote: str = DEFAULT_QUOTE, *, bad_quote: str | None = None, judge_score: int = 5):
    """A `FakeBedrockClient.converse_fn` answering every master-model tool of the scoring pipeline."""

    def fn(*, messages, system, tools, force_tool_use, model_id):
        name = tools[0].name if tools else None
        if name == "record_identity":
            return tool_use_result("record_identity", {"name": "Jane Doe", "email": "jane@example.com"})
        if name == "record_digest":
            import re as _re

            ids = _re.findall(r"^\[(s\d+)\]", messages[0]["content"][0]["text"], flags=_re.MULTILINE)
            return tool_use_result("record_digest", {"items": [{"id": i, "summary": f"summary {i}"} for i in ids]})
        if name == "pick_sections":
            return tool_use_result("pick_sections", {"section_ids": ["s001"]})
        if name == "record_evidence":
            snippets = [{"section_id": "s001", "quote": quote}]
            if bad_quote:
                snippets.append({"section_id": "s001", "quote": bad_quote})
            return tool_use_result("record_evidence", {"snippets": snippets})
        if name == "record_reasoning":
            return tool_use_result("record_reasoning", {"reasoning": "Because the evidence shows it."})
        if name == "map_columns":
            return tool_use_result(
                "map_columns",
                {"email": "Email Address", "name": "Name", "drive_url": "Google Drive URL", "timestamp": "Timestamp"},
            )
        if name == "record_kpi_judgment":
            return tool_use_result(
                "record_kpi_judgment",
                {
                    "matched_level": judge_score,
                    "evidence_quotes": [quote],
                    "reasoning": "GLM fallback.",
                    "score": judge_score,
                },
            )
        return text_result("ok")

    return fn
