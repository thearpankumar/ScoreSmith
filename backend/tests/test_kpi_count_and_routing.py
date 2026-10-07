"""Tests for (A) the cheap request-routing cascade in front of the main-model classification
(app/ai/request_routing.py) and (C) the dynamic / requested KPI count (chunked proposals,
count reconciliation, smarter dedup, truncation handling) in app/ai/scorecard_builder.py.

All model behaviour is scripted through `FakeBedrockClient`/`FakeJevClient`/
`FakeWebSearchClient` (tests/fakes.py) — no live Bedrock, Jev or search call is made. Graph
tests use the shared `_migrated_db` fixture (LangGraph's Postgres checkpointer), like the other
scorecard-builder suites.
"""

from __future__ import annotations

import random
import re
import uuid

import pytest

from app.ai import request_routing as rr
from app.ai import scorecard_builder as sb
from app.ai.bedrock_client import ConverseResult
from app.ai.draft_schema import ScorecardDraft
from app.ai.jev_client import JevChoiceResult
from tests.fakes import (
    FakeBedrockClient,
    FakeJevClient,
    FakeWebSearchClient,
    text_result,
    tool_use_result,
)

# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def _distinct_names(n: int) -> list[str]:
    """n pseudo-word KPI names, pairwise 'distinct' under `_dup_relation` (max pairwise
    SequenceMatcher ratio over 400 seeded names is 0.65 < the 0.72 borderline cut-off)."""
    rng = random.Random(7)

    def word() -> str:
        return "".join(rng.choice("bcdfghklmnprstvz") + rng.choice("aeiou") for _ in range(4)).capitalize()

    return [f"{word()} {word()}" for _ in range(n)]


def _rungs(text: str) -> dict[str, dict]:
    return {str(i): {"qualitative_text": f"{text} level {i}"} for i in range(11)}


def _tools(tools) -> set[str]:
    return {t.name for t in (tools or [])}


_FINISH = {
    "patch": {
        "name": "Hospital Quality",
        "purpose": "Rate hospital quality.",
        "domain": "Healthcare",
        "audience": "Quality leads",
        "target_score": 8,
    },
    "confirmed": True,
    "assistant_message": "Done.",
}

_SUFFICIENT = tool_use_result(
    "assess_research_coverage", {"sufficient": True, "reasoning": "ok", "next_categories": []}
)


class _CountPipeline:
    """Scripted stages for an open-ended fan-out. `n_categories` (None = follow the 'Plan about N
    categories' sizing note, else 3) categories; each can supply at most `capacity` distinct KPIs;
    `first_chunk_limit` caps how many a category returns before saying has_more=False (simulating a
    model that stops early so the reconcile step has to deepen)."""

    def __init__(self, capacity: int, n_categories: int | None = None, first_chunk_limit: int | None = None) -> None:
        self.capacity = capacity
        self.n_categories = n_categories
        self.first_chunk_limit = first_chunk_limit
        self.served: dict[str, int] = {}
        self.names = _distinct_names(400)
        self.cursor = 0
        self.propose_systems: list[str] = []
        self.batch_asks: list[int] = []

    def __call__(self, *, messages, system, tools, force_tool_use, model_id):
        names = _tools(tools)
        if "decide_categories" in names:
            match = re.search(r"Plan about (\d+) categories", system or "")
            n = self.n_categories or (int(match.group(1)) if match else 3)
            cats = [{"name": f"Category {i + 1}", "focus": f"focus {i + 1}"} for i in range(n)]
            return tool_use_result("decide_categories", {"categories": cats})
        if "assess_research_coverage" in names:
            return _SUFFICIENT
        if "record_research_finding" in names:
            return tool_use_result("record_research_finding", {"summary": "s", "suggested_kpis": [], "sources": []})
        if "propose_kpi_batch" in names:
            category = re.search(r'category "([^"]+)"', system or "").group(1)
            ask = int(re.search(r"AT MOST (\d+) KPIs", system or "").group(1))
            self.batch_asks.append(ask)
            done = self.served.get(category, 0)
            deepening = "ALREADY contains" in (system or "")
            limit = self.capacity if (deepening or self.first_chunk_limit is None) else self.first_chunk_limit
            take = max(0, min(ask, limit - done))
            batch = []
            for _ in range(take):
                name = self.names[self.cursor]
                self.cursor += 1
                batch.append({"name": name, "weight": 10, "guidelines": _rungs(name)})
            self.served[category] = done + take
            return tool_use_result("propose_kpi_batch", {"kpis": batch, "has_more": done + take < limit})
        if "judge_duplicate_kpis" in names:
            return tool_use_result("judge_duplicate_kpis", {"same_concept_ids": []})
        self.propose_systems.append(system or "")
        return tool_use_result("update_draft", _FINISH)


def _leaves(draft: dict) -> list[dict]:
    parents = {k["parent_name"] for k in draft["kpis"] if k.get("parent_name")}
    return [k for k in draft["kpis"] if k["name"] not in parents]


async def _run(message: str, pipeline: _CountPipeline):
    fake = FakeBedrockClient(converse_fn=pipeline)
    search = FakeWebSearchClient(search_fn=lambda q: [])
    return await sb.start_session(str(uuid.uuid4()), message, fake, web_search_client=search)


# --------------------------------------------------------------------------------------------
# (C) requested KPI count
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Build me a scorecard with 35 KPIs", 35),
        ("I need around 40 good metrics for ICU quality", 40),
        ("give me forty KPIs", 40),
        ("between 30 and 40 KPIs", 35),
        ("30-40 KPIs please", 35),
        ("a scorecard to review within 30 days", None),
        ("Q3 plan with 3 KPIs", 3),
        ("add 1 KPI", None),
        ("make it 50 KPIs. Actually 45 KPIs", 45),
        # per-group quotas and "top N" selections are not a total scorecard size
        ("5 KPIs per category", None),
        ("Q3 plan with 3 KPIs for each of 6 teams", None),
        ("what are the top 3 KPIs?", None),
        ("30 to 40 KPIs per team", None),
        ("thirty five KPIs", 35),
        ("I want 5 KPIs per team. Overall 30 KPIs", 30),
    ],
)
def test_extract_kpi_target(text: str, expected: int | None) -> None:
    assert rr.extract_kpi_target(text) == expected


def test_old_dedup_dropped_distinct_kpis_that_share_words_now_kept() -> None:
    items = [{"name": n} for n in ("Defect Escape Rate", "Defect Escape Rate by Severity", "Test Coverage",
                                   "Test Coverage of Critical Paths", "Customer Satisfaction Score",
                                   "Customer Satisfaction Trend")]
    kept = sb._dedupe_kpi_batch_items(items)
    assert [i["name"] for i in kept] == [i["name"] for i in items]  # nothing dropped without judge confirmation


def test_dedup_still_drops_obvious_duplicates_and_honours_the_judge() -> None:
    assert [i["name"] for i in sb._dedupe_kpi_batch_items([{"name": "Response Time"}, {"name": "Response Times"}])] == [
        "Response Time"
    ]
    items = [{"name": "Defect Escape Rate"}, {"name": "Defect Escape Rate by Severity"}]
    pair = frozenset({"defect escape rate", "defect escape rate by severity"})
    assert [i["name"] for i in sb._dedupe_kpi_batch_items(items, {pair})] == ["Defect Escape Rate"]


async def test_judge_only_sees_borderline_pairs_and_failures_keep_both() -> None:
    pairs = sb._borderline_pairs(["Defect Escape Rate", "Defect Escape Rate by Severity", "Swift Backlog"])
    assert pairs == [("defect escape rate", "defect escape rate by severity")]

    def boom(**_):
        raise RuntimeError("down")

    cache: dict = {}
    await sb._judge_duplicate_pairs(FakeBedrockClient(dedup_fn=boom), pairs, cache)
    assert cache == {}  # unjudged => not treated as duplicate

    same = FakeBedrockClient(dedup_fn=lambda **_: tool_use_result("judge_duplicate_kpis", {"same_concept_ids": [0]}))
    await sb._judge_duplicate_pairs(same, pairs, cache)
    assert cache == {frozenset(pairs[0]): True}
    await sb._judge_duplicate_pairs(same, pairs, cache)
    assert len(same.dedup_calls) == 1  # cached: never re-judged


def test_plan_fanout_scales_with_target() -> None:
    assert sb._plan_fanout(None) == sb.FanoutPlan()
    plan = sb._plan_fanout(50)
    assert plan.categories == 8 and plan.per_category * plan.categories >= 50
    assert sb._plan_fanout(1000).categories == sb.MAX_CATEGORIES  # safety ceiling only


@pytest.mark.usefixtures("_migrated_db")
async def test_requested_35_kpis_lands_within_two() -> None:
    pipeline = _CountPipeline(capacity=12)  # follows the sizing note: 6 categories
    turn = await _run("Build a hospital quality scorecard with 35 KPIs.", pipeline)
    assert turn.status == "confirmed"
    leaves = _leaves(turn.draft)
    assert 33 <= len(leaves) <= 37, len(leaves)
    assert sum(k["weight"] for k in leaves) == pytest.approx(100.0, abs=0.05)
    assert len({k["parent_name"] for k in leaves}) >= 5  # real category structure
    assert "REQUESTED KPI COUNT" in pipeline.propose_systems[0]


@pytest.mark.usefixtures("_migrated_db")
async def test_requested_50_kpis_lands_within_two() -> None:
    pipeline = _CountPipeline(capacity=12)
    turn = await _run("Build a hospital quality scorecard with 50 KPIs.", pipeline)
    leaves = _leaves(turn.draft)
    assert 48 <= len(leaves) <= 52, len(leaves)
    # No single tool call ever carried more than the per-call quota.
    assert max(pipeline.batch_asks) <= sb.KPIS_PER_BATCH_CALL


@pytest.mark.usefixtures("_migrated_db")
async def test_short_first_pass_triggers_deepening_to_reach_the_target() -> None:
    # Each category stops after 3 KPIs on its own; the reconcile step must deepen to reach 36.
    pipeline = _CountPipeline(capacity=12, n_categories=6, first_chunk_limit=3)
    turn = await _run("Build a hospital quality scorecard with 36 KPIs.", pipeline)
    leaves = _leaves(turn.draft)
    assert 34 <= len(leaves) <= 38, len(leaves)


@pytest.mark.usefixtures("_migrated_db")
async def test_unreachable_count_returns_fewer_with_an_explicit_explanation() -> None:
    # Only 3 categories x 8 distinct KPIs exist; 50 requested: deliver 24, never pad, explain why.
    pipeline = _CountPipeline(capacity=8, n_categories=3)
    turn = await _run("Build a hospital quality scorecard with 50 KPIs.", pipeline)
    leaves = _leaves(turn.draft)
    assert len(leaves) == 24
    prompt = pipeline.propose_systems[0]
    assert "REQUESTED KPI COUNT" in prompt and "could identify only 24" in prompt and "MUST tell the user" in prompt


@pytest.mark.usefixtures("_migrated_db")
async def test_no_count_requested_is_dynamic_and_not_capped_at_the_old_30() -> None:
    pipeline = _CountPipeline(capacity=9, n_categories=5)  # 45 distinct KPIs exist across 5 categories
    turn = await _run("Make me a hospital quality scorecard.", pipeline)
    assert len(_leaves(turn.draft)) == 45 > 30
    assert "REQUESTED KPI COUNT" not in pipeline.propose_systems[0]


@pytest.mark.usefixtures("_migrated_db")
async def test_large_requested_count_fans_out_even_without_web_search() -> None:
    pipeline = _CountPipeline(capacity=12)
    fake = FakeBedrockClient(converse_fn=pipeline)
    turn = await sb.start_session(str(uuid.uuid4()), "Build a hospital quality scorecard with 30 KPIs.", fake,
                                  web_search_client=None)
    assert 28 <= len(_leaves(turn.draft)) <= 32


# --- truncation ---------------------------------------------------------------------------------


async def test_truncated_batch_call_is_retried_with_a_smaller_quota() -> None:
    names = _distinct_names(6)
    calls: list[str] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        calls.append(system)
        if len(calls) == 1:  # cut off mid tool call: partial input must NOT be trusted
            return ConverseResult(stop_reason="max_tokens", tool_name="propose_kpi_batch", tool_input={"kpis": []})
        ask = int(re.search(r"AT MOST (\d+) KPIs", system).group(1))
        kpis = [{"name": n, "weight": 10, "guidelines": _rungs(n)} for n in names[:ask]]
        return tool_use_result("propose_kpi_batch", {"kpis": kpis, "has_more": False})

    out = await sb._propose_category_kpis(
        FakeBedrockClient(converse_fn=fn), None, category="C", focus="f",
        finding=sb.ResearchFinding(category="C", summary="s"), avoid_names=None, want=None,
    )
    assert f"AT MOST {sb.KPIS_PER_BATCH_CALL}" in calls[0] and f"AT MOST {sb.KPIS_PER_BATCH_CALL // 2}" in calls[1]
    assert [k["name"] for k in out] == names[: sb.KPIS_PER_BATCH_CALL // 2]


@pytest.mark.usefixtures("_migrated_db")
async def test_truncated_update_draft_is_discarded_and_retried() -> None:
    seen: list[list] = []

    def fn(*, messages, system, tools, force_tool_use, model_id):
        seen.append(list(messages))
        if len(seen) == 1:
            return ConverseResult(
                stop_reason="max_tokens", tool_name="update_draft",
                tool_input={"patch": {"name": "TRUNCATED-GARBAGE"}, "confirmed": True},
            )
        complete = {**_FINISH, "patch": {**_FINISH["patch"], "kpis": [
            {"name": "Accuracy", "weight": 100, "level": 1, "guidelines": _rungs("acc")}]}}
        return tool_use_result("update_draft", complete)

    turn = await sb.start_session(str(uuid.uuid4()), "Make me a scorecard.", FakeBedrockClient(converse_fn=fn),
                                  web_search_client=None)
    assert turn.status == "confirmed" and turn.draft["name"] == "Hospital Quality"
    assert any("CUT OFF" in (m["content"][0]["text"] if isinstance(m["content"], list) else m["content"])
               for m in seen[1])


@pytest.mark.usefixtures("_migrated_db")
async def test_persistently_truncated_turn_falls_back_to_a_plain_question() -> None:
    fake = FakeBedrockClient(
        converse_fn=lambda **_: ConverseResult(stop_reason="max_tokens", tool_name="update_draft", tool_input={})
    )
    turn = await sb.start_session(str(uuid.uuid4()), "Make me a scorecard.", fake, web_search_client=None)
    assert turn.status == "awaiting_clarification"
    assert "too long to finish" in (turn.assistant_note or "")


def test_bedrock_client_sends_max_tokens_and_reports_truncation(monkeypatch) -> None:
    from app.ai.bedrock_client import BedrockClient, _parse_converse_response

    sent: dict = {}

    class _Raw:
        def converse(self, **kwargs):
            sent.update(kwargs)
            return {"output": {"message": {"content": []}}, "stopReason": "max_tokens"}

    client = BedrockClient()
    client._client = _Raw()
    result = client.converse(messages=[{"role": "user", "content": [{"text": "x"}]}])
    assert sent["inferenceConfig"]["maxTokens"] > 0 and result.truncated
    assert not _parse_converse_response({"stopReason": "end_turn"}).truncated


def test_max_output_tokens_is_clamped_to_the_models_documented_limit(monkeypatch) -> None:
    from app.ai.bedrock_client import BedrockClient, effective_max_output_tokens
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "bedrock_max_output_tokens", 16000)
    assert effective_max_output_tokens("zai.glm-4.7-flash") == 4096  # AWS card: 4K max output
    assert effective_max_output_tokens("zai.glm-5") == 16000
    assert effective_max_output_tokens("some.other-model") == 16000

    sent: list[dict] = []

    class _Raw:
        def converse(self, **kwargs):
            sent.append(kwargs)
            if "inferenceConfig" in kwargs:
                raise RuntimeError("ValidationException: maxTokens exceeds the model limit")
            return {"output": {"message": {"content": []}}, "stopReason": "end_turn"}

    client = BedrockClient()
    client._client = _Raw()
    result = client.converse(messages=[{"role": "user", "content": [{"text": "x"}]}], model_id="some.other-model")
    assert len(sent) == 2 and "inferenceConfig" not in sent[1] and result.stop_reason == "end_turn"


def test_tool_choice_rejection_degrades_to_auto(monkeypatch) -> None:
    from app.ai.bedrock_client import BedrockClient, ToolSpec

    calls: list[dict] = []

    class _Raw:
        def converse(self, **kwargs):
            calls.append(kwargs)
            if "toolChoice" in kwargs.get("toolConfig", {}):
                raise RuntimeError("ValidationException: This model doesn't support the toolChoice field")
            return {"output": {"message": {"content": []}}, "stopReason": "end_turn"}

    client = BedrockClient()
    client._client = _Raw()
    tool = ToolSpec(name="t", description="d", input_schema={"type": "object", "properties": {}})
    client.converse(messages=[{"role": "user", "content": [{"text": "x"}]}], tools=[tool], force_tool_use=True)
    assert "toolChoice" in calls[0]["toolConfig"] and "toolChoice" not in calls[1]["toolConfig"]


def test_plain_newline_list_passes_the_cheap_gate() -> None:
    text = "\n".join(["Scorecard for our SRE team", "MTTR", "Change Failure Rate", "Deploy Frequency", "Page Volume"])
    assert rr.plausibly_contains_kpi_list(text)
    assert not rr.plausibly_contains_kpi_list("make me a scorecard for incident response quality")


def test_prompts_tell_the_model_to_answer_the_users_side_question() -> None:
    spec = {"pinned": [{"name": "A", "parent_name": None}], "allow_additional": False}
    assert "ANSWER it" in sb._format_user_spec_for_prompt(spec)
    assert "ALWAYS answer it" in sb._SYSTEM_PROMPT_TEMPLATE


# --------------------------------------------------------------------------------------------
# (A) routing
# --------------------------------------------------------------------------------------------

# A weight in parentheses keeps this OFF the deterministic fast path (-> model extraction).
_LIST_MESSAGE = "Rate our PRs. KPIs: Review Latency (30%), Test Coverage, Defect Rate"


async def test_no_list_means_zero_model_calls() -> None:
    fake = FakeBedrockClient()
    jev = FakeJevClient()
    text = "Make me a scorecard for incident response quality."
    decision = await rr.route_request(text, bedrock=fake, jev_client=jev)
    assert (decision.mode, decision.source) == ("open_ended", "gate") and decision.skip_extraction
    assert not fake.route_calls and not fake.calls and not jev.choose_calls


async def test_clear_list_is_routed_to_user_specified_by_the_small_model() -> None:
    fake = FakeBedrockClient(route_fn=lambda **_: tool_use_result("route_request", {"mode": "user_specified"}))
    decision = await rr.route_request(_LIST_MESSAGE, bedrock=fake, jev_client=None)
    assert (decision.mode, decision.source) == ("user_specified", "small_model") and not decision.skip_extraction
    assert fake.route_calls[0]["model_id"] == rr.get_settings().judge_model_id  # the small model


async def test_ambiguous_falls_through_to_the_main_model_extraction() -> None:
    fake = FakeBedrockClient()  # default router answer: unsure
    decision = await rr.route_request(_LIST_MESSAGE, bedrock=fake, jev_client=FakeJevClient())
    assert decision.mode == "unsure" and not decision.skip_extraction


async def test_router_failure_falls_through_never_raises() -> None:
    def boom(**_):
        raise RuntimeError("down")

    decision = await rr.route_request(_LIST_MESSAGE, bedrock=FakeBedrockClient(route_fn=boom), jev_client=None)
    assert decision.mode == "unsure" and not decision.skip_extraction


async def test_jev_choice_is_used_when_confident_and_skipped_when_not() -> None:
    fake = FakeBedrockClient(route_fn=lambda **_: tool_use_result("route_request", {"mode": "user_specified"}))
    confident = FakeJevClient(choose_fn=lambda **_: JevChoiceResult("hybrid", 0.9))
    decision = await rr.route_request(_LIST_MESSAGE, bedrock=fake, jev_client=confident)
    assert (decision.mode, decision.source) == ("hybrid", "jev") and not fake.route_calls
    assert set(confident.choose_calls[0]["options"]) == {"user_specified", "hybrid", "open_ended"}

    unsure = FakeJevClient(choose_fn=lambda **_: JevChoiceResult("open_ended", 0.2))
    decision = await rr.route_request(_LIST_MESSAGE, bedrock=fake, jev_client=unsure)
    assert decision.source == "small_model" and len(fake.route_calls) == 1  # fell back to the small model

    def jev_down(**_):
        raise RuntimeError("openrouter down")

    decision = await rr.route_request(
        _LIST_MESSAGE, bedrock=fake, jev_client=FakeJevClient(choose_fn=jev_down)
    )
    assert decision.source == "small_model"


async def test_strong_list_structure_is_never_vetoed_by_a_router() -> None:
    text = "Scorecard for PRs:\n- Review Latency\n- Test Coverage\n- Defect Rate"
    fake = FakeBedrockClient(route_fn=lambda **_: tool_use_result("route_request", {"mode": "open_ended"}))
    decision = await rr.route_request(text, bedrock=fake, jev_client=None)
    assert decision.mode == "unsure" and not fake.route_calls


async def test_router_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr(rr, "get_settings", lambda: type("S", (), {"request_router": "off"})())
    decision = await rr.route_request("Make me a scorecard.", bedrock=FakeBedrockClient(), jev_client=None)
    assert (decision.mode, decision.source) == ("unsure", "forced")


@pytest.mark.usefixtures("_migrated_db")
async def test_graph_no_list_never_calls_the_classifier() -> None:
    fake = FakeBedrockClient(converse_fn=_CountPipeline(capacity=3, n_categories=2))
    await sb.start_session(str(uuid.uuid4()), "Make me a hospital quality scorecard.", fake,
                           web_search_client=FakeWebSearchClient(search_fn=lambda q: []))
    assert not fake.route_calls and not fake.classify_calls


@pytest.mark.usefixtures("_migrated_db")
async def test_graph_clear_list_runs_router_then_extraction_and_keeps_the_users_kpis() -> None:
    def classify(**_):
        return tool_use_result(
            "classify_request",
            {"mode": "user_specified", "reasoning": "r",
             "kpis": [{"name": "Review Latency"}, {"name": "Test Coverage"}, {"name": "Defect Rate"}]},
        )

    pipeline = _CountPipeline(capacity=3, n_categories=2)

    def converse(*, tools, **kw):
        if "fill_user_kpi_details" in _tools(tools):
            wanted = re.findall(r'^- "([^"]+)"', kw["system"] or "", re.MULTILINE)
            return tool_use_result(
                "fill_user_kpi_details",
                {"kpis": [{"name": n, "suggested_weight": 10, "guidelines": _rungs(n)} for n in wanted]},
            )
        if "propose_weighting" in _tools(tools):
            raise RuntimeError("no weighting")
        return pipeline(tools=tools, **kw)

    fake = FakeBedrockClient(
        converse_fn=converse, classify_fn=classify,
        route_fn=lambda **_: tool_use_result("route_request", {"mode": "user_specified"}),
    )
    turn = await sb.start_session(str(uuid.uuid4()), _LIST_MESSAGE, fake, web_search_client=None)
    assert len(fake.route_calls) == 1 and len(fake.classify_calls) == 1
    assert {k["name"] for k in turn.draft["kpis"]} == {"Review Latency", "Test Coverage", "Defect Rate"}


@pytest.mark.usefixtures("_migrated_db")
async def test_graph_classifier_failure_falls_back_to_open_ended() -> None:
    def boom(**_):
        raise RuntimeError("bedrock down")

    fake = FakeBedrockClient(converse_fn=_CountPipeline(capacity=3, n_categories=2), classify_fn=boom)
    turn = await sb.start_session(str(uuid.uuid4()), _LIST_MESSAGE, fake,
                                  web_search_client=FakeWebSearchClient(search_fn=lambda q: []))
    assert turn.status == "confirmed" and len(fake.route_calls) == 1 and len(fake.classify_calls) == 1
    assert all(k["name"] for k in turn.draft["kpis"])  # open-ended fan-out produced the KPIs


async def test_persistent_bedrock_failure_on_a_user_list_fails_the_turn_instead_of_redesigning_it() -> None:
    from app.ai.bedrock_client import BedrockUnavailableError

    def down(**_):
        raise BedrockUnavailableError("throttled")

    fake = FakeBedrockClient(converse_fn=_CountPipeline(capacity=3, n_categories=2), classify_fn=down)
    with pytest.raises(BedrockUnavailableError):
        await sb.start_session(str(uuid.uuid4()), _LIST_MESSAGE, fake,
                               web_search_client=FakeWebSearchClient(search_fn=lambda q: []))
    assert len(fake.classify_calls) == 2  # one retry, then the turn fails


async def test_mid_conversation_gate_and_router_skip_the_extraction() -> None:
    draft = ScorecardDraft.model_validate({"name": "n", "purpose": "p", "domain": "d", "target_score": 7})
    base = {
        "session_id": str(uuid.uuid4()), "draft": draft.model_dump(mode="json"), "user_spec": None,
        "research_findings": None, "user_msgs_checked": 1,
    }

    def state(last: str) -> dict:
        return {**base, "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "q?"},
                                     {"role": "user", "content": last}]}

    fake = FakeBedrockClient()
    await sb.ingest_user_kpis(state("yes, make it stricter"), {"configurable": {"bedrock_client": fake}})
    assert not fake.route_calls and not fake.classify_calls  # gate: nothing model-side at all

    fake = FakeBedrockClient(route_fn=lambda **_: tool_use_result("route_request", {"mode": "open_ended"}))
    msg = "Please focus on the metrics that matter, like quality, speed and cost for the team"
    update = await sb.ingest_user_kpis(state(msg), {"configurable": {"bedrock_client": fake}})
    assert len(fake.route_calls) == 1 and not fake.classify_calls and update == {"user_msgs_checked": 2}


def test_text_result_helper_import_is_used() -> None:
    assert text_result("x").text == "x"
