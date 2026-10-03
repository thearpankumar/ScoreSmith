"""Tests for the user-specified KPI mode (see the "User-specified KPI mode" section of
app/ai/scorecard_builder.py): the request classification step, pinned user KPIs through the
merge/dedup/cap pipeline, weight filling/normalization, `update_draft`'s pinned-KPI
enforcement, and the graph-level behaviour (no open-ended fan-out for a user-supplied list,
hybrid pinning, no-web-search path, classifier failure falling back to open-ended).

Pure-function tests need no database; the graph tests use the shared `_migrated_db` fixture
(LangGraph's Postgres checkpointer), exactly like the other scorecard-builder suites, with
`FakeBedrockClient`/`FakeWebSearchClient` (see tests/fakes.py) — no real Bedrock/network.
"""

from __future__ import annotations

import re
import uuid

import pytest

from app.ai import scorecard_builder as sb
from app.ai.draft_schema import ScorecardDraft
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, tool_use_result

_SUFFICIENT_COVERAGE = tool_use_result(
    "assess_research_coverage", {"sufficient": True, "reasoning": "ok", "next_categories": []}
)


def _rungs(text: str) -> dict[str, dict]:
    return {str(i): {"qualitative_text": f"{text} level {i}"} for i in range(11)}


def _tools(tools) -> set[str]:
    return {t.name for t in (tools or [])}


def _node(name: str, parent: str | None = None, weight: float | None = None, **extra) -> dict:
    return {"name": name, "parent_name": parent, "weight": weight, **extra}


# --- _normalize_user_kpis ---------------------------------------------------------------



@pytest.fixture(autouse=True)
def _model_visit_after_preparation(monkeypatch):
    """These tests exercise the model-visit paths (pinned-KPI protection, retries, ...); the deterministic
    first-turn tail has its own tests in test_first_turn_fast_path.py."""
    monkeypatch.setattr(sb, "FIRST_TURN_FAST_PATH", False)


def test_normalize_keeps_every_user_kpi_beyond_the_research_cap() -> None:
    many = 45
    raw = [_node(f"KPI number {i}") for i in range(many)]
    nodes, notes = sb._normalize_user_kpis(raw)
    assert len(nodes) == many  # a research-style ceiling never applies to user KPIs
    assert [n["name"] for n in nodes] == [f"KPI number {i}" for i in range(many)]
    assert notes == []


def test_normalize_does_not_dedupe_but_suffixes_colliding_names_deterministically() -> None:
    nodes, notes = sb._normalize_user_kpis(
        [_node("Quality"), _node("Speed", "Quality"), _node("quality"), _node("Speed")]
    )
    assert [n["name"] for n in nodes] == ["Quality", "Speed", "quality (2)", "Speed (2)"]
    # parent_name references resolve to the FIRST occurrence of a name.
    assert nodes[1]["parent_name"] == "Quality"
    assert len(notes) == 2


def test_normalize_recomputes_levels_and_clamps_depth_without_losing_kpis() -> None:
    raw = [
        _node("L1"),
        _node("L2", "L1"),
        _node("L3", "L2"),
        _node("L4", "L3"),
        _node("L5", "L4"),
        _node("L6", "L5"),
        _node("Orphan", "No Such Parent"),
        _node("Cycle A", "Cycle B"),
        _node("Cycle B", "Cycle A"),
    ]
    nodes, notes = sb._normalize_user_kpis(raw)
    by_name = {n["name"]: n for n in nodes}
    assert [by_name[n]["level"] for n in ("L1", "L2", "L3", "L4")] == [1, 2, 3, 4]
    # Deeper than max depth: re-attached under its level-3 ancestor, still present, still level 4.
    assert by_name["L5"]["level"] == by_name["L6"]["level"] == 4
    assert by_name["L5"]["parent_name"] == by_name["L6"]["parent_name"] == "L3"
    assert by_name["Orphan"]["parent_name"] is None
    assert len(nodes) == 9
    ScorecardDraft.model_validate({"kpis": [{k: v for k, v in n.items() if k != "guidance"} for n in nodes]})
    assert any("circular" in n for n in notes)


def test_normalize_keeps_only_a_complete_user_rubric() -> None:
    nodes, _ = sb._normalize_user_kpis(
        [
            _node("Full", guidelines=_rungs("full")),
            _node("Partial", guidelines={"0": {"qualitative_text": "bad"}, "10": "great"}, user_guidance="be strict"),
        ]
    )
    assert len(nodes[0]["guidelines"]) == 11
    assert nodes[1]["guidelines"] == {}
    assert "bad" in nodes[1]["guidance"] and "be strict" in nodes[1]["guidance"]


# --- _resolve_user_weights ---------------------------------------------------------------


def _weights(nodes: list[dict]) -> dict[str, float | None]:
    return {n["name"]: n["weight"] for n in nodes}


def _resolved(raw: list[dict], suggested: dict[str, float] | None = None):
    nodes, _ = sb._normalize_user_kpis(raw)
    notes = sb._resolve_user_weights(nodes, suggested or {})
    return nodes, notes


def test_weights_given_and_summing_to_100_are_untouched() -> None:
    nodes, notes = _resolved([_node("A", weight=70), _node("B", weight=30)])
    assert _weights(nodes) == {"A": 70.0, "B": 30.0}
    assert notes == []


def test_weights_not_summing_to_100_are_scaled_proportionally_and_reported() -> None:
    nodes, notes = _resolved([_node("A", weight=60), _node("B", weight=40), _node("C", weight=20)])
    w = _weights(nodes)
    assert sum(w.values()) == pytest.approx(100.0)
    assert w["A"] / w["B"] == pytest.approx(1.5, rel=1e-2)
    assert any("scaled" in n for n in notes)


def test_missing_weights_take_the_remainder_equally_or_by_research_suggestion() -> None:
    nodes, notes = _resolved([_node("A", weight=50), _node("B"), _node("C")])
    assert _weights(nodes) == {"A": 50.0, "B": 25.0, "C": 25.0}
    assert any("filled" in n for n in notes)

    nodes, _ = _resolved([_node("A", weight=40), _node("B"), _node("C")], {"B": 30.0, "C": 10.0})
    w = _weights(nodes)
    assert w["A"] == pytest.approx(40.0) and w["B"] == pytest.approx(45.0) and w["C"] == pytest.approx(15.0)

    nodes, _ = _resolved([_node("A"), _node("B"), _node("C"), _node("D")])
    assert set(_weights(nodes).values()) == {25.0}


def test_category_weights_make_leaf_weights_relative_and_categories_carry_none() -> None:
    nodes, _ = _resolved(
        [
            _node("Cat1", weight=70),
            _node("Cat2", weight=30),
            _node("a", "Cat1", 50),
            _node("b", "Cat1", 50),
            _node("c", "Cat2"),
        ]
    )
    w = _weights(nodes)
    assert w["Cat1"] is None and w["Cat2"] is None
    assert w["a"] == pytest.approx(35.0) and w["b"] == pytest.approx(35.0) and w["c"] == pytest.approx(30.0)


def test_excluded_from_scoring_leaves_stay_out_of_the_sum() -> None:
    nodes, _ = _resolved([_node("A"), _node("B"), _node("Info", included_in_scoring=False)])
    w = _weights(nodes)
    assert w["A"] + w["B"] == pytest.approx(100.0) and w["Info"] is None


# --- merge with pinned KPIs --------------------------------------------------------------


def _finding(category: str, names: list[str]) -> sb.ResearchFinding:
    kpis = [{"name": n, "weight": 50, "level": 2, "parent_name": category, "guidelines": _rungs(n)} for n in names]
    return sb.ResearchFinding(category=category, summary="s", proposed_kpis=kpis)


def _pinned_nodes(names_weights: list[tuple[str, float]]) -> list[dict]:
    return [
        {
            "name": n, "weight": w, "level": 1, "parent_name": None,
            "included_in_scoring": True, "guidelines": _rungs(n),
        }
        for n, w in names_weights
    ]


def test_merge_keeps_pinned_exactly_drops_research_duplicates_and_sums_to_100() -> None:
    pinned = _pinned_nodes([("Defect Rate", 60), ("Review Coverage", 40)])
    findings = [_finding("Delivery", ["Defect Rate Percentage", "Lead Time", "Deployment Frequency"])]
    merged = sb._merge_research_kpi_batches(findings, {}, pinned=pinned)
    by_name = {k["name"]: k for k in merged}

    assert "Defect Rate Percentage" not in by_name  # near-duplicate of a user KPI: research side dropped
    assert {"Defect Rate", "Review Coverage", "Lead Time", "Deployment Frequency", "Delivery"} == set(by_name)
    # User KPIs: exact names/hierarchy/guidelines, relative weight proportion (60:40) preserved.
    assert by_name["Defect Rate"]["guidelines"] == _rungs("Defect Rate")
    assert by_name["Defect Rate"]["weight"] / by_name["Review Coverage"]["weight"] == pytest.approx(1.5, rel=1e-2)
    assert by_name["Defect Rate"]["level"] == 1 and by_name["Lead Time"]["level"] == 2
    assert by_name["Delivery"]["weight"] is None
    leaf_total = sum(k["weight"] for k in merged if k["weight"] is not None)
    assert leaf_total == pytest.approx(100.0, abs=0.02)
    # Research leaves collectively capped at HYBRID_MAX_RESEARCH_WEIGHT_SHARE.
    research_total = by_name["Lead Time"]["weight"] + by_name["Deployment Frequency"]["weight"]
    assert research_total == pytest.approx(sb.HYBRID_MAX_RESEARCH_WEIGHT_SHARE, abs=0.02)
    ScorecardDraft.model_validate({"kpis": merged})


def test_merge_cap_never_drops_user_kpis_even_when_they_exceed_it() -> None:
    n_user = 40
    pinned = _pinned_nodes([(f"User metric {i:02d} alpha{i * 7}", 100 / n_user) for i in range(n_user)])
    research_names = ["Throughput", "Burn Rate", "Escalation Clarity", "Stakeholder Trust", "Cost Variance"]
    findings = [_finding("Cat A", research_names)]
    merged = sb._merge_research_kpi_batches(findings, {}, pinned=pinned)
    names = {k["name"] for k in merged}
    assert {p["name"] for p in pinned} <= names
    assert len(names) == n_user + 5 + 1


def test_merge_reuses_a_user_category_and_renames_a_clashing_research_category() -> None:
    pinned = [
        {"name": "Quality", "weight": None, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": {}},
        {"name": "Accuracy", "weight": 100.0, "level": 2, "parent_name": "Quality", "included_in_scoring": True,
         "guidelines": _rungs("Accuracy")},
        {"name": "Speed", "weight": None, "level": 1, "parent_name": None, "included_in_scoring": True,
         "guidelines": _rungs("Speed")},
    ]
    pinned[2]["weight"] = 0.0  # a user LEAF literally named like a research category
    pinned[1]["weight"] = 100.0
    findings = [_finding("Quality", ["Recall Depth"]), _finding("Speed", ["Throughput Rate"])]
    merged = sb._merge_research_kpi_batches(findings, {}, pinned=pinned)
    by_name = {k["name"]: k for k in merged}
    assert [k["name"] for k in merged].count("Quality") == 1  # user's category reused, not duplicated
    assert by_name["Recall Depth"]["parent_name"] == "Quality" and by_name["Recall Depth"]["level"] == 2
    assert "Speed (suggested)" in by_name and by_name["Throughput Rate"]["parent_name"] == "Speed (suggested)"
    assert by_name["Speed"]["parent_name"] is None and by_name["Speed"]["guidelines"] == _rungs("Speed")
    ScorecardDraft.model_validate({"kpis": merged})


def test_merge_without_pinned_is_the_unchanged_open_ended_behaviour() -> None:
    findings = [_finding("Cat A", ["Alpha Metric", "Beta Metric"]), _finding("Cat B", ["Gamma Metric"])]
    merged = sb._merge_research_kpi_batches(findings, {})
    assert {k["name"] for k in merged} == {"Cat A", "Cat B", "Alpha Metric", "Beta Metric", "Gamma Metric"}
    assert sum(k["weight"] for k in merged if k["weight"] is not None) == pytest.approx(100.0, abs=0.02)


# --- fill_user_kpi_details can't add/rename KPIs ------------------------------------------


def test_filled_details_are_matched_to_user_names_and_extras_discarded() -> None:
    out = sb._validate_filled_user_kpis(
        [
            {"name": "defect-rate", "suggested_weight": 30, "guidelines": _rungs("d")},
            {"name": "Invented KPI", "suggested_weight": 90, "guidelines": _rungs("x")},
            {"name": "Broken", "suggested_weight": 10, "guidelines": {"99": {"qualitative_text": "bad key"}}},
        ],
        ["Defect Rate", "Broken"],
    )
    assert set(out) == {"Defect Rate"}  # normalized exact match only; invented/invalid dropped
    assert out["Defect Rate"]["suggested_weight"] == 30


# --- update_draft pinned-KPI enforcement ---------------------------------------------------


def _state_with_pinned(extra_messages: list[dict] | None = None) -> dict:
    draft = ScorecardDraft.model_validate(
        {
            "name": "N", "purpose": "P", "domain": "D", "target_score": 8,
            "kpis": [
                {"name": "Alpha", "weight": 50, "level": 1, "guidelines": _rungs("a")},
                {"name": "Beta", "weight": 50, "level": 1, "guidelines": _rungs("b")},
            ],
        }
    )
    return {
        "draft": draft.model_dump(mode="json"),
        "messages": [{"role": "user", "content": "My KPIs are Alpha and Beta"}, *(extra_messages or [])],
        "user_spec": {
            "mode": "user_specified",
            "pinned": [
                {"name": "Alpha", "parent_name": None, "level": 1},
                {"name": "Beta", "parent_name": None, "level": 1},
            ],
            "notes": [],
            "allow_additional": False,
        },
    }


def _patch_state(state: dict, patch: dict, **tool_input) -> dict:
    return {**state, "pending_tool": {"name": "update_draft", "input": {"patch": patch, **tool_input}}}


def _kpi(name: str, weight: float, parent: str | None = None, level: int = 1) -> dict:
    return {"name": name, "weight": weight, "level": level, "parent_name": parent, "guidelines": _rungs(name)}


def test_update_draft_rejects_dropping_or_renaming_a_pinned_kpi() -> None:
    state = _state_with_pinned()
    dropped = sb.update_draft(_patch_state(state, {"kpis": [_kpi("Alpha", 100)]}))
    assert "REJECTED" in dropped["messages"][0]["content"] and '"Beta"' in dropped["messages"][0]["content"]
    assert "draft" not in dropped  # state untouched

    renamed = sb.update_draft(_patch_state(state, {"kpis": [_kpi("Alpha", 50), _kpi("Beta Renamed", 50)]}))
    assert "REJECTED" in renamed["messages"][0]["content"]

    moved = sb.update_draft(
        _patch_state(state, {"kpis": [_kpi("Alpha", 100), _kpi("Beta", 0, "Alpha", 2)]})
    )
    assert "REJECTED" in moved["messages"][0]["content"] and "moved" in moved["messages"][0]["content"]


def test_update_draft_allows_pinned_kpis_kept_additions_and_scalar_only_patches() -> None:
    state = _state_with_pinned()
    ok = sb.update_draft(_patch_state(state, {"kpis": [_kpi("Alpha", 40), _kpi("Beta", 40), _kpi("Gamma", 20)]}))
    assert "draft" in ok and len(ok["draft"]["kpis"]) == 3
    assert "user_spec" not in ok  # nothing about the pinned set changed
    scalar = sb.update_draft(_patch_state(state, {"audience": "Everyone"}))
    assert scalar["draft"]["audience"] == "Everyone" and len(scalar["draft"]["kpis"]) == 2


def test_update_draft_lets_the_user_explicitly_remove_a_pinned_kpi_and_releases_the_pin() -> None:
    state = _state_with_pinned([{"role": "user", "content": "Please remove Beta from the scorecard."}])
    result = sb.update_draft(
        _patch_state(state, {"kpis": [_kpi("Alpha", 100)]}, user_requested_kpi_changes=["Beta"])
    )
    assert [k["name"] for k in result["draft"]["kpis"]] == ["Alpha"]
    assert [p["name"] for p in result["user_spec"]["pinned"]] == ["Alpha"]
    # ...and a later patch is no longer blocked from omitting it (it is no longer pinned).
    state2 = {**state, "draft": result["draft"], "user_spec": result["user_spec"]}
    assert "draft" in sb.update_draft(_patch_state(state2, {"kpis": [_kpi("Alpha", 100)]}))


def test_update_draft_pins_a_list_the_user_pastes_mid_conversation_but_not_invented_kpis() -> None:
    state = {
        "draft": ScorecardDraft().model_dump(mode="json"),
        "messages": [{"role": "user", "content": "Use these KPIs: Latency, Error Budget"}],
        "user_spec": None,
    }
    patch = {"kpis": [_kpi("Latency", 40), _kpi("Error Budget", 30), _kpi("Model Invented", 30)]}
    result = sb.update_draft(
        _patch_state(state, patch, user_specified_kpis=["Latency", "Error Budget", "Model Invented"])
    )
    pinned = {p["name"] for p in result["user_spec"]["pinned"]}
    assert pinned == {"Latency", "Error Budget"}  # "Model Invented" never appears in a user message

    state2 = {**state, "draft": result["draft"], "user_spec": result["user_spec"]}
    rejected = sb.update_draft(_patch_state(state2, {"kpis": [_kpi("Latency", 50), _kpi("Model Invented", 50)]}))
    assert "REJECTED" in rejected["messages"][0]["content"]


def test_prompt_block_lists_pinned_kpis_and_is_empty_for_open_ended() -> None:
    assert sb._format_user_spec_for_prompt(None) == ""
    block = sb._format_user_spec_for_prompt(
        {
            "pinned": [{"name": "Alpha", "parent_name": None}, {"name": "Beta", "parent_name": "Alpha"}],
            "notes": ["Weights scaled."],
            "allow_additional": False,
        }
    )
    assert '"Alpha"' in block and '"Beta" (under "Alpha")' in block and "Weights scaled." in block
    assert "user_requested_kpi_changes" in block


# --- Graph-level behaviour -----------------------------------------------------------------

_USER_MESSAGE = (
    "We review pull requests. Use exactly these KPIs: under Quality — Defect Rate (50%) and Review Coverage "
    "(30%); and On-time Delivery. Fill in the rest."
)
_USER_KPIS = [
    {"name": "Quality", "parent_name": None},
    {"name": "Defect Rate", "parent_name": "Quality", "weight": 50},
    {"name": "Review Coverage", "parent_name": "Quality", "weight": 30},
    {"name": "On-time Delivery", "parent_name": None},
]
_LEAVES = ["Defect Rate", "Review Coverage", "On-time Delivery"]


def _classify(mode: str = "user_specified", **extra):
    def classify_fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result(
            "classify_request",
            {"mode": mode, "reasoning": "r", "kpis": _USER_KPIS, "purpose": "Rate PR quality.", **extra},
        )

    return classify_fn


class _Pipeline:
    """Scripted Bedrock behaviour for every stage, recording what each stage saw."""

    def __init__(self, propose_responses: list | None = None, weighting: dict | None = None) -> None:
        self.weighting = weighting
        self.stage_calls: list[str] = []
        self.propose_systems: list[str] = []
        self.propose_messages: list[list] = []
        self._propose = list(propose_responses or [])

    def __call__(self, *, messages, system, tools, force_tool_use, model_id):
        names = _tools(tools)
        if "decide_categories" in names:
            self.stage_calls.append("decide_categories")
            return tool_use_result(
                "decide_categories", {"categories": [{"name": "Delivery", "focus": "delivery performance"}]}
            )
        if "assess_research_coverage" in names:
            return _SUFFICIENT_COVERAGE
        if "record_research_finding" in names:
            self.stage_calls.append("record_research_finding")
            return tool_use_result(
                "record_research_finding",
                {
                    "summary": "benchmarks",
                    "suggested_kpis": [{"name": "Rogue Suggestion", "rationale": "x"}],
                    "suggested_thresholds": [{"metric": "defects", "value_or_range": "<2%"}],
                    "sources": [],
                },
            )
        if "propose_kpi_batch" in names:
            self.stage_calls.append("propose_kpi_batch")
            return tool_use_result(
                "propose_kpi_batch",
                {
                    "kpis": [
                        {"name": "Defect Rate Percentage", "weight": 40, "guidelines": _rungs("dup")},
                        {"name": "Lead Time", "weight": 30, "guidelines": _rungs("lt")},
                        {"name": "Deployment Frequency", "weight": 30, "guidelines": _rungs("df")},
                    ]
                },
            )
        if "propose_weighting" in names:
            self.stage_calls.append("propose_weighting")
            if self.weighting is None:
                raise RuntimeError("weighting model unavailable")
            return tool_use_result("propose_weighting", self.weighting)
        if "fill_user_kpi_details" in names:
            self.stage_calls.append("fill_user_kpi_details")
            wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
            kpis = [{"name": n, "suggested_weight": 10, "guidelines": _rungs(n)} for n in wanted]
            kpis.append({"name": "Invented By Model", "suggested_weight": 99, "guidelines": _rungs("x")})
            return tool_use_result("fill_user_kpi_details", {"kpis": kpis})
        # propose_kpis
        self.stage_calls.append("propose_kpis")
        self.propose_systems.append(system or "")
        self.propose_messages.append(list(messages))
        if self._propose:
            nxt = self._propose.pop(0)
            return nxt if not isinstance(nxt, dict) else tool_use_result("update_draft", nxt)
        return tool_use_result("update_draft", _FINISH)


_FINISH = {
    "patch": {
        "name": "PR Quality",
        "purpose": "Rate PR quality.",
        "domain": "Engineering",
        "audience": "Eng leads",
        "target_score": 8,
    },
    "confirmed": True,
    "assistant_message": "Done.",
}


async def _state(session_id: str) -> dict:
    compiled = await sb.get_graph_manager().get_compiled_graph()
    snapshot = await compiled.aget_state({"configurable": {"thread_id": session_id}})
    return dict(snapshot.values)


@pytest.mark.usefixtures("_migrated_db")
async def test_user_specified_mode_skips_the_open_ended_fanout_and_preserves_the_users_kpis() -> None:
    session_id = str(uuid.uuid4())
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify())
    search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(session_id, _USER_MESSAGE, fake, web_search_client=search)

    assert turn.status == "confirmed"
    assert "decide_categories" not in pipeline.stage_calls and "propose_kpi_batch" not in pipeline.stage_calls
    assert "fill_user_kpi_details" in pipeline.stage_calls
    kpis = turn.draft["kpis"]
    assert [k["name"] for k in kpis] == ["Quality", "Defect Rate", "Review Coverage", "On-time Delivery"]
    by_name = {k["name"]: k for k in kpis}
    assert by_name["Defect Rate"]["parent_name"] == "Quality" and by_name["Defect Rate"]["level"] == 2
    assert by_name["Quality"]["weight"] is None and by_name["Quality"]["guidelines"] == {}
    # User weights kept; the missing one takes the remainder (50 + 30 + 20 = 100).
    assert by_name["Defect Rate"]["weight"] == pytest.approx(50.0)
    assert by_name["Review Coverage"]["weight"] == pytest.approx(30.0)
    assert by_name["On-time Delivery"]["weight"] == pytest.approx(20.0)
    for leaf in _LEAVES:
        assert len(by_name[leaf]["guidelines"]) == 11
    assert "Invented By Model" not in by_name  # enrichment cannot add KPIs
    assert turn.draft["purpose"] == "Rate PR quality."  # scalar the user stated was applied

    state = await _state(session_id)
    assert {p["name"] for p in state["user_spec"]["pinned"]} == {k["name"] for k in kpis}
    # Research ran for the user's KPIs (scoped), but its KPI suggestions were stripped.
    assert state["research_findings"] and all(f["suggested_kpis"] == [] for f in state["research_findings"])
    # propose_kpis was told the KPI set is user-authoritative.
    assert "USER-SPECIFIED KPIs" in pipeline.propose_systems[0] and '"Defect Rate"' in pipeline.propose_systems[0]


@pytest.mark.usefixtures("_migrated_db")
async def test_user_specified_mode_works_without_web_search() -> None:
    session_id = str(uuid.uuid4())
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify())

    turn = await sb.start_session(session_id, _USER_MESSAGE, fake, web_search_client=None)

    assert turn.status == "confirmed"
    assert "record_research_finding" not in pipeline.stage_calls and "decide_categories" not in pipeline.stage_calls
    by_name = {k["name"]: k for k in turn.draft["kpis"]}
    assert set(by_name) == {"Quality", "Defect Rate", "Review Coverage", "On-time Delivery"}
    assert all(len(by_name[leaf]["guidelines"]) == 11 for leaf in _LEAVES)  # from the model's own knowledge


@pytest.mark.usefixtures("_migrated_db")
async def test_hybrid_mode_pins_user_kpis_and_researches_complementary_ones() -> None:
    session_id = str(uuid.uuid4())
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify("hybrid", wants_more_kpis=True))
    search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(session_id, _USER_MESSAGE + " Also suggest more.", fake, web_search_client=search)

    assert turn.status == "confirmed"
    assert "decide_categories" in pipeline.stage_calls and "propose_kpi_batch" in pipeline.stage_calls
    names = [k["name"] for k in turn.draft["kpis"]]
    for user_name in ("Quality", "Defect Rate", "Review Coverage", "On-time Delivery"):
        assert user_name in names
    assert "Defect Rate Percentage" not in names  # research near-duplicate of a user KPI dropped
    assert {"Lead Time", "Deployment Frequency", "Delivery"} <= set(names)
    leaf_weights = {k["name"]: k["weight"] for k in turn.draft["kpis"] if k["weight"] is not None}
    assert sum(leaf_weights.values()) == pytest.approx(100.0, abs=0.02)
    assert leaf_weights["Defect Rate"] / leaf_weights["Review Coverage"] == pytest.approx(50 / 30, rel=1e-2)

    state = await _state(session_id)
    assert {"Quality", "Defect Rate", "Review Coverage", "On-time Delivery"} == {
        p["name"] for p in state["user_spec"]["pinned"]
    }
    assert state["user_spec"]["allow_additional"] is True


@pytest.mark.usefixtures("_migrated_db")
@pytest.mark.parametrize("failure", ["raises", "wrong_tool", "malformed"])
async def test_classification_failure_falls_back_to_the_open_ended_pipeline(failure: str) -> None:
    session_id = str(uuid.uuid4())
    pipeline = _Pipeline()

    def classify_fn(*, messages, system, tools, force_tool_use, model_id):
        if failure == "raises":
            raise RuntimeError("bedrock down")
        if failure == "wrong_tool":
            return tool_use_result("update_draft", {"patch": {}})
        return tool_use_result("classify_request", {"mode": "user_specified", "kpis": "not a list"})

    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=classify_fn)
    search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(session_id, _USER_MESSAGE, fake, web_search_client=search)

    assert turn.status == "confirmed"
    assert "decide_categories" in pipeline.stage_calls  # the existing open-ended fan-out ran
    assert "fill_user_kpi_details" not in pipeline.stage_calls
    assert (await _state(session_id)).get("user_spec") is None


@pytest.mark.usefixtures("_migrated_db")
async def test_open_ended_classification_runs_the_existing_pipeline_unchanged() -> None:
    session_id = str(uuid.uuid4())
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline)  # default fake classification: open_ended
    search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(session_id, "Make me a scorecard for incident response quality.", fake,
                                  web_search_client=search)

    # No list-like content: the heuristic gate answers open_ended with ZERO extra model calls
    # (no router call, no main-model classification) — see request_routing.py.
    assert turn.status == "confirmed" and not fake.classify_calls and not fake.route_calls
    assert pipeline.stage_calls[0] == "decide_categories"
    assert "Pinned KPIs:" not in pipeline.propose_systems[0]  # no user-spec block for an open-ended session


@pytest.mark.usefixtures("_migrated_db")
async def test_a_patch_that_drops_a_user_kpi_is_rejected_and_the_model_retries() -> None:
    session_id = str(uuid.uuid4())
    bad_patch = {
        "patch": {"kpis": [_kpi("Defect Rate", 100)], "name": "x"},
        "assistant_message": "Simplified.",
    }
    pipeline = _Pipeline(propose_responses=[bad_patch])
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify())

    turn = await sb.start_session(session_id, _USER_MESSAGE, fake, web_search_client=None)

    assert turn.status == "confirmed"
    assert {k["name"] for k in turn.draft["kpis"]} == {"Quality", "Defect Rate", "Review Coverage", "On-time Delivery"}
    retry_context = " ".join(
        block["text"] for m in pipeline.propose_messages[1] for block in m["content"]
    )  # the second propose_kpis call saw the server's rejection
    assert "REJECTED" in retry_context and "preserved exactly" in retry_context


# --- Verified escape hatch (user_requested_kpi_changes) ------------------------------------------


def _msgs(*pairs: tuple[str, str]) -> list[dict]:
    return [{"role": role, "content": text} for role, text in pairs]


def test_model_claimed_change_without_the_user_asking_is_rejected_with_a_retry_message() -> None:
    state = _state_with_pinned()  # the user only ever listed their KPIs; never asked for a change
    result = sb.update_draft(_patch_state(state, {"kpis": [_kpi("Alpha", 100)]}, user_requested_kpi_changes=["Beta"]))
    text = result["messages"][0]["content"]
    assert "REJECTED" in text and "could not find the user asking" in text and "ask_clarification" in text
    assert "draft" not in result

    # A change-intent word alone is not enough: the KPI must be named in the user's message.
    state2 = _state_with_pinned([{"role": "user", "content": "Please drop the second one."}])
    result2 = sb.update_draft(_patch_state(state2, {"kpis": [_kpi("Alpha", 100)]}, user_requested_kpi_changes=["Beta"]))
    assert "REJECTED" in result2["messages"][0]["content"]
    # ...and neither is merely naming it without asking for any change.
    state3 = _state_with_pinned([{"role": "user", "content": "Beta looks great to me."}])
    result3 = sb.update_draft(_patch_state(state3, {"kpis": [_kpi("Alpha", 100)]}, user_requested_kpi_changes=["Beta"]))
    assert "REJECTED" in result3["messages"][0]["content"]


def test_a_short_yes_to_the_assistants_confirmation_question_verifies_the_change() -> None:
    state = _state_with_pinned(
        [
            {"role": "assistant", "content": "Do you want me to remove Beta from the scorecard?"},
            {"role": "user", "content": "yes"},
        ]
    )
    result = sb.update_draft(_patch_state(state, {"kpis": [_kpi("Alpha", 100)]}, user_requested_kpi_changes=["Beta"]))
    assert [k["name"] for k in result["draft"]["kpis"]] == ["Alpha"]


def test_a_legit_rename_is_accepted_and_releases_the_old_pin() -> None:
    state = _state_with_pinned([{"role": "user", "content": "Rename Beta to Gamma please."}])
    patch = {"kpis": [_kpi("Alpha", 50), _kpi("Gamma", 50)]}
    result = sb.update_draft(_patch_state(state, patch, user_requested_kpi_changes=["Beta"]))
    assert {k["name"] for k in result["draft"]["kpis"]} == {"Alpha", "Gamma"}
    assert {p["name"] for p in result["user_spec"]["pinned"]} == {"Alpha"}


# --- User-supplied weights/guidelines are protected; model-filled ones are not -------------------


def _alpha(weight: float) -> dict:
    return {**_kpi("Alpha", weight), "guidelines": _rungs("a")}  # the pinned Alpha's own rubric


def _protected_state(extra_messages: list[dict] | None = None) -> dict:
    state = _state_with_pinned(extra_messages)
    pins = state["user_spec"]["pinned"]
    pins[0]["weight"] = 50.0  # Alpha: the USER gave this weight
    pins[1]["weight"] = None  # Beta: model-filled — freely editable
    canonical = {
        k: {"qualitative_text": v["qualitative_text"], "quantitative_criteria": None} for k, v in _rungs("a").items()
    }
    pins[0]["guidelines_hash"] = sb._guidelines_hash(canonical)
    return state


def test_user_supplied_weight_cannot_be_silently_changed_but_filled_weights_can() -> None:
    state = _protected_state()
    changed = sb.update_draft(_patch_state(state, {"kpis": [_alpha(60), _kpi("Beta", 40)]}))
    text = changed["messages"][0]["content"]
    assert "REJECTED" in text and "user-supplied weight" in text and "ONLY weights you filled in" in text

    # Adding a KPI by shrinking only the model-filled weight is fine (and leaves the pin alone).
    ok = sb.update_draft(_patch_state(state, {"kpis": [_alpha(50), _kpi("Beta", 30), _kpi("Gamma", 20)]}))
    assert "draft" in ok and "user_spec" not in ok


def test_user_supplied_guidelines_cannot_be_silently_rewritten() -> None:
    state = _protected_state()
    rewritten = {**_alpha(50), "guidelines": _rungs("totally different")}
    rejected = sb.update_draft(_patch_state(state, {"kpis": [rewritten, _kpi("Beta", 50)]}))
    assert "REJECTED" in rejected["messages"][0]["content"] and "guidelines" in rejected["messages"][0]["content"]
    # Rewriting the MODEL-filled Beta is fine.
    ok = sb.update_draft(
        _patch_state(state, {"kpis": [_alpha(50), {**_kpi("Beta", 50), "guidelines": _rungs("new beta")}]})
    )
    assert "draft" in ok


def test_a_verified_user_reweight_is_allowed_and_refreezes_the_new_weight() -> None:
    state = _protected_state([{"role": "user", "content": "Make Alpha weigh more - 70 please."}])
    patch = {"kpis": [_alpha(70), _kpi("Beta", 30)]}
    ok = sb.update_draft(_patch_state(state, patch, user_requested_kpi_changes=["Alpha"]))
    assert "draft" in ok
    alpha = next(p for p in ok["user_spec"]["pinned"] if p["name"] == "Alpha")
    assert alpha["weight"] == 70.0

    # The same patch with an unverified claim (user never asked) is rejected.
    bad = sb.update_draft(_patch_state(_protected_state(), patch, user_requested_kpi_changes=["Alpha"]))
    assert "REJECTED" in bad["messages"][0]["content"]


def test_pin_state_records_only_user_supplied_weights_and_guidelines() -> None:
    nodes, _ = sb._normalize_user_kpis(
        [_node("A", weight=60), _node("B"), _node("C", guidelines=_rungs("c"), weight=40)]
    )
    spec = sb.UserSpec(mode="user_specified", kpis=nodes)
    final = [
        {"name": "A", "weight": 60.0, "level": 1, "parent_name": None, "guidelines": _rungs("a")},
        {"name": "B", "weight": 0.0, "level": 1, "parent_name": None, "guidelines": _rungs("b")},
        {"name": "C", "weight": 40.0, "level": 1, "parent_name": None, "guidelines": nodes[2]["guidelines"]},
    ]
    pins = {p["name"]: p for p in sb._user_spec_state(spec, final, [])["pinned"]}
    assert pins["A"]["weight"] == 60.0 and pins["A"]["guidelines_hash"] is None
    assert pins["B"]["weight"] is None and pins["B"]["guidelines_hash"] is None
    assert pins["C"]["weight"] == 40.0 and pins["C"]["guidelines_hash"] == sb._guidelines_hash(nodes[2]["guidelines"])


# --- Mid-conversation lists -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", False),
        ("looks good, save it", False),
        ("make it stricter", False),
        ("- Latency\n- Error budget\n- Saturation", True),
        ("1. Latency\n2) Error budget", True),
        ("Please use these KPIs: latency, error budget and saturation", True),
        ("Add: Latency, Error Budget, Saturation", True),
        ("I like the metrics", False),
    ],
)
def test_kpi_list_gate_is_cheap_and_permissive(text: str, expected: bool) -> None:
    assert sb._plausibly_contains_kpi_list(text) is expected


def test_rebalance_changes_only_flexible_weights_when_possible() -> None:
    leaves = [{"name": "u", "weight": 50.0}, {"name": "m1", "weight": 30.0}, {"name": "m2", "weight": 40.0}]
    assert sb._rebalance_leaves(leaves, {"u"}) is False
    w = {leaf["name"]: leaf["weight"] for leaf in leaves}
    assert w["u"] == 50.0 and w["m1"] + w["m2"] == pytest.approx(50.0)
    assert w["m2"] / w["m1"] == pytest.approx(4 / 3, rel=1e-2)


def test_rebalance_scales_user_weights_only_as_a_last_resort() -> None:
    leaves = [{"name": "u1", "weight": 80.0}, {"name": "u2", "weight": 60.0}]
    assert sb._rebalance_leaves(leaves, {"u1", "u2"}) is True
    assert sum(leaf["weight"] for leaf in leaves) == pytest.approx(100.0)


def test_normalize_attaches_a_new_list_under_existing_draft_kpis_and_clamps_depth() -> None:
    nodes, _ = sb._normalize_user_kpis(
        [_node("Child", "Alpha"), _node("Grandchild", "Child"), _node("Under Deep", "Deep")],
        {"alpha": ("Alpha", 1), "deep": ("Deep", 4)},
    )
    by_name = {n["name"]: n for n in nodes}
    assert (by_name["Child"]["parent_name"], by_name["Child"]["level"]) == ("Alpha", 2)
    assert (by_name["Grandchild"]["parent_name"], by_name["Grandchild"]["level"]) == ("Child", 3)
    assert by_name["Under Deep"]["parent_name"] is None and by_name["Under Deep"]["level"] == 1


def _followup(draft: ScorecardDraft, user_spec: dict | None, raw: list[dict], overlaps: list[dict] | None = None):
    existing = {k.name.casefold(): (k.name, k.level) for k in draft.kpis}
    nodes, notes = sb._normalize_user_kpis(raw, existing)
    spec = sb.UserSpec(mode="user_specified", kpis=nodes, overlaps=overlaps or [], notes=notes)
    new_kpis = [
        {
            "name": n["name"], "weight": n["weight"], "level": n["level"], "parent_name": n["parent_name"],
            "included_in_scoring": True, "guidelines": _rungs(n["name"]),
        }
        for n in nodes
    ]
    return sb._merge_followup_into_draft(draft, user_spec, spec, sb.PinnedKpis(kpis=new_kpis, findings=[], notes=notes))


def _draft_ab() -> ScorecardDraft:
    return ScorecardDraft.model_validate(
        {"kpis": [_kpi("Alpha", 50), _kpi("Beta", 50)], "name": "n", "purpose": "p", "domain": "d", "target_score": 7}
    )


def test_followup_list_rebalances_only_model_filled_weights_and_freezes_the_new_user_weight() -> None:
    user_spec = {"pinned": [{"name": "Beta", "parent_name": None, "level": 1, "weight": 50.0}], "notes": []}
    kpis, pins, _notes = _followup(_draft_ab(), user_spec, [_node("Gamma", weight=20)])
    w = {k["name"]: k["weight"] for k in kpis}
    assert w["Beta"] == 50.0 and w["Gamma"] == 20.0 and w["Alpha"] == pytest.approx(30.0)  # only Alpha moved
    assert sum(w.values()) == pytest.approx(100.0)
    pin = {p["name"]: p for p in pins}
    assert pin["Gamma"]["weight"] == 20.0 and pin["Beta"]["weight"] == 50.0 and "Alpha" not in pin


def test_followup_overlap_applies_the_users_weight_and_nesting_under_a_leaf_makes_it_a_category() -> None:
    kpis, pins, _notes = _followup(_draft_ab(), None, [], overlaps=[{"name": "Alpha", "weight": 40}])
    w = {k["name"]: k["weight"] for k in kpis}
    assert w["Alpha"] == 40.0 and w["Beta"] == 60.0 and len(kpis) == 2
    assert {p["name"]: p["weight"] for p in pins}["Alpha"] == 40.0

    kpis, _pins, notes = _followup(_draft_ab(), None, [_node("Kid A", "Alpha"), _node("Kid B", "Alpha")])
    by_name = {k["name"]: k for k in kpis}
    assert by_name["Alpha"]["weight"] is None and by_name["Alpha"]["guidelines"] == {}
    assert by_name["Kid A"]["level"] == 2 and by_name["Kid A"]["parent_name"] == "Alpha"
    assert sum(k["weight"] for k in kpis if k["weight"] is not None) == pytest.approx(100.0)
    assert any("no longer carries its own weight" in n for n in notes)
    ScorecardDraft.model_validate({"kpis": kpis})


def _ingest_state(draft: ScorecardDraft, messages: list[dict], checked: int | None = 1) -> dict:
    return {
        "session_id": str(uuid.uuid4()), "messages": messages, "draft": draft.model_dump(mode="json"),
        "user_spec": None, "research_findings": None, "user_msgs_checked": checked,
    }


def _classify_with(kpis: list[dict], mode: str = "user_specified"):
    def classify_fn(*, messages, system, tools, force_tool_use, model_id):
        return tool_use_result(
            "classify_request", {"mode": mode, "reasoning": "r", "kpis": kpis, "purpose": "Rate PRs."}
        )

    return classify_fn


async def test_ingest_ignores_ordinary_messages_and_runs_at_most_once_per_message() -> None:
    pipeline = _Pipeline()
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify_with([{"name": "Gamma"}]))
    cfg = {"configurable": {"bedrock_client": fake}}

    chatty = _ingest_state(_draft_ab(), _msgs(("user", "hi"), ("assistant", "q?"), ("user", "yes")))
    assert await sb.ingest_user_kpis(chatty, cfg) == {"user_msgs_checked": 2}
    assert fake.classify_calls == []  # the heuristic gate kept ordinary chat free

    listy = _ingest_state(_draft_ab(), _msgs(("user", "hi"), ("assistant", "q?"), ("user", "Add: Gamma, Delta, Eps")))
    update = await sb.ingest_user_kpis(listy, cfg)
    assert len(fake.classify_calls) == 1 and "Gamma" in {k["name"] for k in update["draft"]["kpis"]}
    again = await sb.ingest_user_kpis({**listy, **update}, cfg)
    assert again == {} and len(fake.classify_calls) == 1  # idempotent per message


async def test_ingest_failure_leaves_the_draft_untouched() -> None:
    def boom(*, messages, system, tools, force_tool_use, model_id):
        raise RuntimeError("bedrock down")

    fake = FakeBedrockClient(converse_fn=_Pipeline(), classify_fn=boom)
    state = _ingest_state(_draft_ab(), _msgs(("user", "hi"), ("assistant", "q?"), ("user", "Add: Gamma, Delta, Eps")))
    update = await sb.ingest_user_kpis(state, {"configurable": {"bedrock_client": fake}})
    assert update == {"user_msgs_checked": 2}


@pytest.mark.usefixtures("_migrated_db")
async def test_a_kpi_list_pasted_mid_conversation_is_pinned_enriched_and_weighted_in_the_graph() -> None:
    session_id = str(uuid.uuid4())
    calls = {"n": 0}

    def classify_fn(*, messages, system, tools, force_tool_use, model_id):
        calls["n"] += 1  # the first message has no list: the gate skips classification, so this is the paste
        return tool_use_result(
            "classify_request",
            {
                "mode": "user_specified", "reasoning": "r",
                "kpis": [
                    {"name": "Latency", "weight": 30}, {"name": "Error Budget", "weight": 20}, {"name": "Saturation"},
                ],
            },
        )

    ask = tool_use_result("ask_clarification", {"question": "Anything specific in mind?", "missing_fields": ["kpis"]})
    pipeline = _Pipeline(propose_responses=[ask])
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=classify_fn)

    first = await sb.start_session(session_id, "Make me a scorecard for our API.", fake, web_search_client=None)
    assert first.status == "awaiting_clarification"
    turn = await sb.send_message(
        session_id, "Use these KPIs: Latency (30%), Error Budget (20%), Saturation", fake, web_search_client=None
    )

    assert turn.status == "confirmed" and calls["n"] == 1
    by_name = {k["name"]: k for k in turn.draft["kpis"]}
    assert set(by_name) == {"Latency", "Error Budget", "Saturation"}
    assert by_name["Latency"]["weight"] == 30.0 and by_name["Error Budget"]["weight"] == 20.0
    assert by_name["Saturation"]["weight"] == pytest.approx(50.0)
    assert all(len(k["guidelines"]) == 11 for k in by_name.values())
    state = await _state(session_id)
    pins = {p["name"]: p for p in state["user_spec"]["pinned"]}
    assert set(pins) == set(by_name) and pins["Latency"]["weight"] == 30.0 and pins["Saturation"]["weight"] is None
    assert "Pinned KPIs:" in pipeline.propose_systems[-1]


# --- Dedicated weighting / formula step ------------------------------------------------------------

_FLAT_KPIS = [{"name": "Defect Rate"}, {"name": "Review Coverage"}, {"name": "On-time Delivery"}]
_GATING = 'min(kpi["Defect Rate"], kpi["Review Coverage"]) * 0.7 + kpi["On-time Delivery"] * 0.3'


def _weighting(formula: str | None = _GATING) -> dict:
    return {
        "approach": "risk/impact-based",
        "weights": [
            {"name": "Defect Rate", "relative_importance": 60, "rationale": "escaped defects are costly"},
            {"name": "Review Coverage", "relative_importance": 30, "rationale": "reduces defect risk"},
            {"name": "On-time Delivery", "relative_importance": 10, "rationale": "least harmful when late"},
        ],
        "scoring_formula": formula,
        "formula_rationale": "quality KPIs must not be offset by speed",
    }


@pytest.mark.usefixtures("_migrated_db")
async def test_weighting_step_proposes_weights_and_a_validated_formula_with_rationale() -> None:
    session_id = str(uuid.uuid4())
    pipeline = _Pipeline(weighting=_weighting())
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify_with(_FLAT_KPIS))
    search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(session_id, _USER_MESSAGE, fake, web_search_client=search)

    w = {k["name"]: k["weight"] for k in turn.draft["kpis"]}
    assert w == {
        "Defect Rate": pytest.approx(60.0),
        "Review Coverage": pytest.approx(30.0),
        "On-time Delivery": pytest.approx(10.0),
    }
    assert turn.draft["scoring_formula"] == _GATING
    assert "propose_weighting" in pipeline.stage_calls
    assert pipeline.stage_calls.count("record_research_finding") == 1  # one shared research agent
    prompt = pipeline.propose_systems[0]
    assert "risk/impact-based" in prompt and "escaped defects are costly" in prompt
    assert "custom scoring formula" in prompt and "quality KPIs must not be offset by speed" in prompt


@pytest.mark.usefixtures("_migrated_db")
async def test_a_proposed_formula_that_skips_a_kpi_or_is_invalid_is_not_applied() -> None:
    for formula in ('min(kpi["Defect Rate"], kpi["Review Coverage"])', 'kpi["Nope"] * 1'):
        pipeline = _Pipeline(weighting=_weighting(formula))
        fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify_with(_FLAT_KPIS))
        turn = await sb.start_session(str(uuid.uuid4()), _USER_MESSAGE, fake, web_search_client=None)
        assert turn.draft["scoring_formula"] is None
        assert {k["name"]: k["weight"] for k in turn.draft["kpis"]}["Defect Rate"] == pytest.approx(60.0)


@pytest.mark.usefixtures("_migrated_db")
async def test_weighting_step_never_overrides_user_weights_or_applies_a_formula_alongside_them() -> None:
    kpis = [{"name": "Defect Rate", "weight": 50}, {"name": "Review Coverage"}, {"name": "On-time Delivery"}]
    pipeline = _Pipeline(weighting=_weighting())
    fake = FakeBedrockClient(converse_fn=pipeline, classify_fn=_classify_with(kpis))
    turn = await sb.start_session(str(uuid.uuid4()), _USER_MESSAGE, fake, web_search_client=None)
    w = {k["name"]: k["weight"] for k in turn.draft["kpis"]}
    assert w["Defect Rate"] == 50.0  # user-given weight untouched
    assert w["Review Coverage"] == pytest.approx(37.5) and w["On-time Delivery"] == pytest.approx(12.5)  # 3:1
    assert turn.draft["scoring_formula"] is None


@pytest.mark.usefixtures("_migrated_db")
async def test_weighting_failure_falls_back_to_default_fill_and_no_web_search_skips_its_research() -> None:
    failing = _Pipeline(weighting=None)  # propose_weighting raises
    fake = FakeBedrockClient(converse_fn=failing, classify_fn=_classify_with(_FLAT_KPIS))
    turn = await sb.start_session(str(uuid.uuid4()), _USER_MESSAGE, fake, web_search_client=None)
    assert turn.status == "confirmed"
    weights = [k["weight"] for k in turn.draft["kpis"]]
    assert sum(weights) == pytest.approx(100.0, abs=0.02) and max(weights) - min(weights) < 0.02  # equal shares
    assert turn.draft["scoring_formula"] is None

    no_web = _Pipeline(weighting=_weighting())
    fake2 = FakeBedrockClient(converse_fn=no_web, classify_fn=_classify_with(_FLAT_KPIS))
    await sb.start_session(str(uuid.uuid4()), _USER_MESSAGE, fake2, web_search_client=None)
    assert "propose_weighting" in no_web.stage_calls and "record_research_finding" not in no_web.stage_calls
