"""Latency fixes for user-specified KPI sessions (35 user KPIs took 20-30+ minutes live):
deterministic list parsing instead of a GLM-5 extraction, no Jev quality gate / retries in
user mode, resilient small-chunk guideline writing (timeout -> half-size retry -> singles ->
marked fallback), bounded research, no web_search in the first propose step, per-phase timing
events, the botocore client config, and cancelling an abandoned turn. Bedrock is a fake."""

from __future__ import annotations

import re
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app.ai.scorecard_builder as sb
from app.ai import request_routing as rr
from app.ai.bedrock_client import BedrockTimeoutError, build_botocore_config
from app.config import get_settings
from app.deps import get_bedrock_client, get_jev_client, get_web_search_client
from app.main import app
from tests.fakes import (
    FakeBedrockClient,
    FakeJevClient,
    FakeWebSearchClient,
    search_result,
    text_result,
    tool_use_result,
)

# --- deterministic list parser ----------------------------------------------------------------


def _names(items):
    return [i["name"] for i in items]


def test_parser_reads_bullets_and_numbered_lists_exactly_and_ignores_the_surrounding_prose() -> None:
    text = (
        "I'm setting up a scorecard for support. Here are my KPIs, please use exactly these:\n"
        "- First Response Time\n* Customer Effort Score\n• Net Promoter Score\n\n"
        "I need 3 KPIs. Also, why is this scorecard needed?"
    )
    items = rr.parse_structured_kpi_list(text)
    assert _names(items) == ["First Response Time", "Customer Effort Score", "Net Promoter Score"]
    assert all(i["parent_name"] is None for i in items)
    numbered = rr.parse_structured_kpi_list("KPIs:\n1. Alpha Rate\n2) Beta Rate\n3. Gamma Rate")
    assert _names(numbered) == ["Alpha Rate", "Beta Rate", "Gamma Rate"]


def test_parser_builds_the_hierarchy_from_indentation() -> None:
    text = "My KPIs:\n- Quality:\n  - Defect Rate\n  - Review Coverage\n- Delivery\n  - Lead Time\n    - Queue Time\n"
    items = rr.parse_structured_kpi_list(text)
    assert [(i["name"], i["parent_name"]) for i in items] == [
        ("Quality", None),
        ("Defect Rate", "Quality"),
        ("Review Coverage", "Quality"),
        ("Delivery", None),
        ("Lead Time", "Delivery"),
        ("Queue Time", "Lead Time"),
    ]


def test_parser_reads_a_plain_comma_list_after_a_kpi_cue() -> None:
    items = rr.parse_structured_kpi_list("Rate our PRs. My KPIs are: Review Latency, Test Coverage and Defect Rate.")
    assert _names(items) == ["Review Latency", "Test Coverage", "Defect Rate"]


@pytest.mark.parametrize(
    "text",
    [
        "KPIs:\n- Defect Rate (30%)\n- Review Coverage\n- Lead Time",  # weight/parentheses
        "KPIs:\n- Defect Rate: fewer than 2% escape\n- Review Coverage\n- Lead Time",  # definition
        "KPIs:\n- Defect Rate\n- Review Coverage\n- Lead Time\n\nPlease suggest more KPIs too.",  # hybrid
        "KPIs:\n- Defect Rate\n- Review Coverage\n- Lead Time\nI need 30 KPIs.",  # wants more than listed
        "KPIs:\n- Defect Rate\nsome prose in between\n- Review Coverage\n- Lead Time",  # prose between items
        "KPIs:\n- Defect Rate\n- Review Coverage",  # too short to trust
        "Make me a scorecard for incident response quality.",
    ],
)
def test_parser_declines_anything_unclear_so_the_model_extraction_runs(text: str) -> None:
    assert rr.parse_structured_kpi_list(text) is None


# --- botocore configuration -------------------------------------------------------------------


def test_botocore_config_has_a_long_read_timeout_retries_and_a_pool_larger_than_the_concurrency() -> None:
    cfg = build_botocore_config()
    settings = get_settings()
    assert cfg.read_timeout == settings.bedrock_read_timeout_seconds >= 120  # botocore default is 60
    assert cfg.connect_timeout == settings.bedrock_connect_timeout_seconds
    assert cfg.retries["mode"] == "standard" and cfg.retries["max_attempts"] >= 1
    assert cfg.max_pool_connections >= settings.bedrock_max_concurrency + 8  # default 10 overflowed at 8 + threads


def test_read_timeouts_surface_as_a_dedicated_error_that_is_still_a_bedrock_unavailable_error() -> None:
    import botocore.exceptions as be

    from app.ai.bedrock_client import BedrockUnavailableError, _raise_unavailable

    with pytest.raises(BedrockTimeoutError) as info:
        _raise_unavailable("Converse", be.ReadTimeoutError(endpoint_url="https://x"))
    assert isinstance(info.value, BedrockUnavailableError)


# --- resilient guideline writing ----------------------------------------------------------------


def _rungs(text: str) -> dict:
    return {str(i): {"qualitative_text": f"{text} level {i}"} for i in range(11)}


def _chunk(n: int) -> list[dict]:
    return [{"name": f"Signal {i}", "parent_name": None, "guidance": ""} for i in range(n)]


def _fill_fn(seen: list[int], should_time_out):
    def converse(*, system, **_kw):
        wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
        seen.append(len(wanted))
        if should_time_out(wanted):
            raise BedrockTimeoutError("read timeout")
        return tool_use_result(
            "fill_user_kpi_details",
            {"kpis": [{"name": n, "suggested_weight": 5, "guidelines": _rungs(n)} for n in wanted]},
        )

    return converse



@pytest.fixture(autouse=True)
def _model_visit_after_preparation(monkeypatch):
    """These tests exercise the model visit after preparation; the deterministic first-turn tail has its
    own tests in test_first_turn_fast_path.py."""
    monkeypatch.setattr(sb, "FIRST_TURN_FAST_PATH", False)


async def test_a_timed_out_chunk_recovers_by_retrying_at_half_the_size() -> None:
    seen: list[int] = []
    tried: set[frozenset[str]] = set()

    def first_try_times_out(wanted):  # every full-size call times out (once per distinct KPI set)
        key = frozenset(wanted)
        if len(wanted) < 3 or key in tried:
            return False
        tried.add(key)
        return True

    fake = FakeBedrockClient(converse_fn=_fill_fn(seen, first_try_times_out))
    chunk = _chunk(3)
    got, fallback = await sb._fill_chunk_resilient(
        fake, None, "ctx", chunk, None, deadline=time.monotonic() + 100
    )
    assert fallback == []
    assert all(len(got[n["name"]]["guidelines"]) == 11 for n in chunk)
    assert seen[0] == 3 and 2 in seen  # full chunk first, then a half-size retry (ceil(3/2) = 2)


async def test_size_dependent_timeouts_end_in_single_kpi_calls_and_still_no_fallback() -> None:
    seen: list[int] = []
    fake = FakeBedrockClient(converse_fn=_fill_fn(seen, lambda wanted: len(wanted) > 1))
    chunk = _chunk(3)
    got, fallback = await sb._fill_chunk_resilient(
        fake, None, "ctx", chunk, None, deadline=time.monotonic() + 100
    )
    assert fallback == [] and all(len(got[n["name"]]["guidelines"]) == 11 for n in chunk)
    assert seen.count(1) == 3  # the singles that finally succeeded


async def test_when_every_call_fails_the_kpis_get_a_clearly_marked_complete_fallback_rubric() -> None:
    fake = FakeBedrockClient(converse_fn=_fill_fn([], lambda wanted: True))
    chunk = _chunk(2)
    got, fallback = await sb._fill_chunk_resilient(
        fake, None, "ctx", chunk, None, deadline=time.monotonic() + 100
    )
    assert fallback == ["Signal 0", "Signal 1"]
    for n in chunk:
        rubric = got[n["name"]]["guidelines"]
        assert len(rubric) == 11 and "Auto-generated fallback rubric" in rubric["5"]["qualitative_text"]


async def test_past_the_phase_deadline_remaining_kpis_skip_research_context_and_go_straight_to_singles() -> None:
    systems: list[str] = []
    seen: list[int] = []
    inner = _fill_fn(seen, lambda wanted: len(wanted) > 1)

    def converse(*, system, **kw):
        systems.append(system or "")
        return inner(system=system, **kw)

    finding = sb.ResearchFinding(category="C", summary="UNIQUE-RESEARCH-SUMMARY")
    got, fallback = await sb._fill_chunk_resilient(
        FakeBedrockClient(converse_fn=converse), None, "ctx", _chunk(3), [finding], deadline=time.monotonic() - 1
    )
    assert fallback == [] and seen.count(1) == 3
    assert "UNIQUE-RESEARCH-SUMMARY" in systems[0] and all("UNIQUE-RESEARCH-SUMMARY" not in s for s in systems[1:])


# --- whole pipeline ---------------------------------------------------------------------------

_NAMES = [f"Signal {i}" for i in range(1, 36)]
_MESSAGE = (
    "I'm setting up a quality scorecard for our customer support organization. "
    "Here are my KPIs, please use exactly these:\n"
    + "\n".join(f"- {n}" for n in _NAMES)
    + "\n\nI need 35 KPIs. Also, why is this scorecard needed in the first place?"
)


class _UserModePipeline:
    def __init__(self, propose: list) -> None:
        self.propose = list(propose)
        self.tool_names: list[set[str]] = []
        self.propose_tools: list[set[str]] = []
        self.searches_by_agent = 0
        self.fill_sizes: list[int] = []

    def __call__(self, *, messages, system, tools, force_tool_use, model_id):
        names = {t.name for t in tools or []}
        self.tool_names.append(names)
        if "fill_user_kpi_details" in names:
            wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
            self.fill_sizes.append(len(wanted))
            return tool_use_result(
                "fill_user_kpi_details",
                {"kpis": [{"name": n, "suggested_weight": 5, "guidelines": _rungs(n)} for n in wanted]},
            )
        if "propose_weighting" in names:
            return tool_use_result(
                "propose_weighting",
                {
                    "approach": "risk based",
                    "weights": [{"name": n, "relative_importance": 10, "rationale": "r"} for n in _NAMES],
                },
            )
        if "record_research_finding" in names:
            if "web_search" in names:
                self.searches_by_agent += 1
                return tool_use_result("web_search", {"query": "support KPI benchmarks"})
            return tool_use_result(
                "record_research_finding",
                {"summary": "s", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )
        if "decide_categories" in names or "propose_kpi_batch" in names or "assess_research_coverage" in names:
            raise AssertionError("the open-ended fan-out must not run for a user-specified list")
        self.propose_tools.append(names)
        nxt = self.propose.pop(0)
        return nxt


_SCALARS = {
    "patch": {
        "name": "Support Quality",
        "purpose": "Keep support quality measurable.",
        "domain": "Customer Support",
        "audience": "Support leads",
        "target_score": 8,
    },
    "confirmed": True,
    "assistant_message": "It is needed because ... Done.",
}


@pytest.mark.usefixtures("_migrated_db")
async def test_35_bullet_kpis_skip_extraction_and_gate_use_small_calls_and_time_every_phase(monkeypatch) -> None:
    events: list[tuple[str, str, str]] = []

    async def record(session_id, turn_started_at, actor, event_type, message, *, round=1):
        events.append((actor, event_type, message))

    monkeypatch.setattr(sb, "emit_turn_event", record)
    # The header (name/purpose/domain/audience/target) is filled early, so propose has nothing to ask.
    pipeline = _UserModePipeline([tool_use_result("update_draft", {"patch": {}, "confirmed": True,
                                                                     "assistant_message": "Needed because..."})])
    fake = FakeBedrockClient(converse_fn=pipeline)  # classify/route default fakes would FAIL the test if called
    search = FakeWebSearchClient(search_fn=lambda q: [search_result("T", "https://x.example", "snippet")])
    jev = FakeJevClient(rate_fn=lambda **_: 0.0)  # would force endless quality_gate_retry if the gate ran

    turn = await sb.start_session(
        str(uuid.uuid4()), _MESSAGE, fake, web_search_client=search, jev_client=jev,
        turn_started_at=sb.datetime.now(),  # any non-None value: events are captured by the recorder
    )

    assert not fake.classify_calls and not fake.route_calls  # deterministic parse, zero model extraction
    assert turn.status == "confirmed"
    assert [k["name"] for k in turn.draft["kpis"]] == _NAMES  # exact names and order
    assert all(len(k["guidelines"]) == 11 for k in turn.draft["kpis"])
    assert sum(k["weight"] for k in turn.draft["kpis"]) == pytest.approx(100.0, abs=0.05)
    assert jev.calls == []  # no Jev gate anywhere in user mode (research agents, weighting, propose)
    assert not any(e[1] in ("quality_gate", "quality_gate_retry") for e in events)
    per_call = get_settings().user_kpis_per_fill_call
    assert max(pipeline.fill_sizes) <= per_call and len(pipeline.fill_sizes) == -(-35 // per_call)
    assert pipeline.searches_by_agent <= 3  # <= 3 research agents, one search each
    assert len(pipeline.propose_tools) == 1
    assert turn.draft["name"] and turn.draft["purpose"] and turn.draft["audience"] and turn.draft["target_score"]
    assert all("web_search" not in names for names in pipeline.propose_tools)  # no search in the first propose
    timing = " ".join(m for a, t, m in events if a == "master" and t == "timing")
    for phase in ("classify", "enrich", "weighting", "reconcile"):
        assert f"Phase '{phase}' took" in timing


@pytest.mark.usefixtures("_migrated_db")
async def test_every_guideline_call_timing_out_once_still_yields_a_complete_draft() -> None:
    names = _NAMES[:6]  # two full-size chunks of 3
    message = "Support scorecard. KPIs:\n" + "\n".join(f"- {n}" for n in names)
    tried: set[frozenset[str]] = set()
    inner = _UserModePipeline([tool_use_result("update_draft", _SCALARS)])

    def converse(*, tools, system, **kw):
        if "fill_user_kpi_details" in {t.name for t in tools or []}:
            wanted = frozenset(re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE))
            if len(wanted) >= get_settings().user_kpis_per_fill_call and wanted not in tried:
                tried.add(wanted)  # every full-size guideline call times out once
                raise BedrockTimeoutError("read timeout")
        if "propose_weighting" in {t.name for t in tools or []}:
            return tool_use_result(
                "propose_weighting",
                {"approach": "a", "weights": [{"name": n, "relative_importance": 10, "rationale": "r"} for n in names]},
            )
        return inner(tools=tools, system=system, **kw)

    turn = await sb.start_session(str(uuid.uuid4()), message, FakeBedrockClient(converse_fn=converse),
                                  web_search_client=None, jev_client=FakeJevClient())
    assert turn.status == "confirmed"
    assert all(len(k["guidelines"]) == 11 for k in turn.draft["kpis"])
    assert not any(
        "Auto-generated fallback" in k["guidelines"]["5"]["qualitative_text"] for k in turn.draft["kpis"]
    )  # recovered by the split retry, not by the placeholder


# --- abandoned turns -----------------------------------------------------------------------------


@pytest.fixture
def _inert_externals():
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()
    yield
    for dep in (get_bedrock_client, get_web_search_client, get_jev_client):
        app.dependency_overrides.pop(dep, None)


def test_deleting_a_session_cancels_its_running_background_turn(
    client: TestClient, seed_user_id: str, _inert_externals
) -> None:
    from app.api.v1 import chat as chat_mod

    user = client.post(
        "/api/v1/users", json={"email": f"cancel-{uuid.uuid4().hex[:6]}@example.com", "name": "C"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    release = threading.Event()
    calls = {"n": 0}

    def converse(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return text_result("Some Title")
        release.wait(timeout=20)  # the turn is stuck in a (slow) model call
        return tool_use_result("ask_clarification", {"question": "q?", "options": [], "missing_fields": []})

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=converse)
    sid = str(uuid.uuid4())
    try:
        r = client.post(
            "/api/v1/chat/sessions?wait=false", json={"message": "Build a scorecard.", "session_id": sid},
            headers={"X-User-Id": user["id"]},
        )
        assert r.status_code == 202
        deadline = time.monotonic() + 10
        while calls["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls["n"] >= 2 and chat_mod._turn_tasks  # the turn is running
        assert client.delete(f"/api/v1/chat/sessions/{sid}", headers={"X-User-Id": user["id"]}).status_code == 204
        assert not chat_mod._turn_tasks  # task cancelled and unregistered
        assert client.get(f"/api/v1/chat/sessions/{sid}").status_code == 404
    finally:
        release.set()


# --- early header ---------------------------------------------------------------------------------

_HEADER_MSG = (
    "I'm setting up a quality scorecard for our customer support organization. "
    "Here are my KPIs, please use exactly these:\n- Alpha Rate\n- Beta Rate\n- Gamma Rate\n\n"
    "Also, why is this scorecard needed?"
)


def test_deterministic_header_derives_a_name_purpose_domain_audience_and_default_target() -> None:
    h = sb._derive_header(_HEADER_MSG)
    assert h["name"] == "Customer Support Quality Scorecard" and h["domain"] == "Customer Support"
    assert h["purpose"].startswith("I'm setting up a quality scorecard") and "why is" not in h["purpose"]
    assert h["audience"] and h["target_score"] == 8.0


async def test_header_fill_never_overwrites_user_values_and_falls_back_when_the_small_call_fails() -> None:
    def boom(**_):
        raise BedrockTimeoutError("slow")

    out = await sb._fill_header(
        FakeBedrockClient(header_fn=boom), _HEADER_MSG, ["Alpha Rate"], {"name": "My Name", "target_score": 9.0}
    )
    assert out["name"] == "My Name" and out["target_score"] == 9.0  # user-given kept
    assert all(out[k] not in (None, "") for k in sb.HEADER_FIELDS)  # rest derived deterministically

    smart = FakeBedrockClient(
        header_fn=lambda **_: tool_use_result(
            "set_scorecard_header",
            {"name": "Support QA", "purpose": "P", "domain": "D", "audience": "A", "target_score": 7},
        )
    )
    out2 = await sb._fill_header(smart, _HEADER_MSG, [], {"purpose": "User purpose"})
    assert out2["purpose"] == "User purpose" and out2["name"] == "Support QA" and out2["target_score"] == 7.0


@pytest.mark.usefixtures("_migrated_db")
async def test_header_is_set_and_visible_mid_turn_even_when_enrichment_blows_up(monkeypatch) -> None:
    session_id = str(uuid.uuid4())

    async def explode(*a, **k):
        raise RuntimeError("enrichment blew up")

    monkeypatch.setattr(sb, "_prepare_pinned_kpis", explode)
    with pytest.raises(RuntimeError):
        await sb.start_session(session_id, _HEADER_MSG, FakeBedrockClient(), web_search_client=None)
    # The graph never checkpointed, yet the preview GET path still sees header + the user's KPIs.
    turn = await sb.get_session_state(session_id)
    assert turn is not None
    assert turn.draft["name"] == "Customer Support Quality Scorecard" and turn.draft["target_score"] == 8.0
    assert turn.draft["purpose"] and turn.draft["audience"] and turn.draft["domain"]
    assert [k["name"] for k in turn.draft["kpis"]] == ["Alpha Rate", "Beta Rate", "Gamma Rate"]


@pytest.mark.usefixtures("_migrated_db")
async def test_final_draft_keeps_the_early_header_even_if_propose_never_fills_it() -> None:
    session_id = str(uuid.uuid4())
    names = ["Alpha Rate", "Beta Rate", "Gamma Rate"]

    def converse(*, tools, system, **_kw):
        tn = {t.name for t in tools or []}
        if "fill_user_kpi_details" in tn:
            wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
            return tool_use_result(
                "fill_user_kpi_details",
                {"kpis": [{"name": n, "suggested_weight": 5, "guidelines": _rungs(n)} for n in wanted]},
            )
        if "propose_weighting" in tn:
            return tool_use_result(
                "propose_weighting",
                {"approach": "a", "weights": [{"name": n, "relative_importance": 5, "rationale": "r"} for n in names]},
            )
        return tool_use_result("update_draft", {"patch": {}, "confirmed": True, "assistant_message": "ok"})

    turn = await sb.start_session(
        session_id, _HEADER_MSG, FakeBedrockClient(converse_fn=converse), web_search_client=None
    )
    assert turn.draft["name"] and turn.draft["purpose"] and turn.draft["domain"] and turn.draft["audience"]
    assert turn.draft["target_score"] == 8.0
