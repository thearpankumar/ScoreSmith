"""Compact guideline format + scaling of the user-KPI enrichment: the model emits terse
`levels` + numeric `thresholds` (few output tokens), the rubric is expanded deterministically,
chunk count stays bounded as KPIs grow, the weighting call never queues behind the fill
fan-out, and finished KPIs are published to the live preview as they complete."""

from __future__ import annotations

import re
import threading
import time
import uuid

import pytest

import app.ai.scorecard_builder as sb
from app.ai.bedrock_client import BedrockTimeoutError
from app.config import get_settings
from tests.fakes import FakeBedrockClient, tool_use_result


def _compact(name: str, *, direction: str = "higher_better", thresholds=None, levels=None) -> dict:
    return {
        "name": name,
        "suggested_weight": 5,
        "metric": f"{name} metric",
        "unit": "%",
        "direction": direction,
        "levels": levels if levels is not None else [f"{name}: condition for level {i}" for i in range(11)],
        "thresholds": thresholds if thresholds is not None else [i * 10 for i in range(11)],
    }


def _validate(item: dict, name: str = "K"):
    return sb._validate_filled_user_kpis([item], [name]).get(name)


def test_compact_rubric_expands_to_11_distinct_levels_with_monotonic_quantitative_criteria() -> None:
    got = _validate(_compact("K"))
    rub = got["guidelines"]
    assert sorted(rub, key=int) == [str(i) for i in range(11)]
    assert len({r["qualitative_text"] for r in rub.values()}) == 11
    values = [rub[str(i)]["quantitative_criteria"]["value"] for i in range(11)]
    assert values == sorted(values) and values[0] != values[-1]
    assert rub["5"]["quantitative_criteria"] == {"metric": "K metric", "operator": ">=", "value": 50, "unit": "%"}


def test_lower_better_uses_le_and_descending_thresholds() -> None:
    got = _validate(_compact("K", direction="lower_better", thresholds=[100 - i * 10 for i in range(11)]))
    crit = got["guidelines"]["10"]["quantitative_criteria"]
    assert crit["operator"] == "<=" and crit["value"] == 0


@pytest.mark.parametrize(
    "thresholds",
    [[0, 5, 3, 9, 10, 11, 12, 13, 14, 15, 16], [None] * 11, [1] * 11],
)
def test_non_monotonic_or_empty_thresholds_drop_quantitative_criteria_but_keep_the_text(thresholds) -> None:
    got = _validate(_compact("K", thresholds=thresholds))
    assert len(got["guidelines"]) == 11
    assert all(r["quantitative_criteria"] is None for r in got["guidelines"].values())


def test_direction_none_is_purely_qualitative() -> None:
    got = _validate(_compact("K", direction="none", thresholds=[None] * 11))
    assert all(r["quantitative_criteria"] is None for r in got["guidelines"].values())


def test_wrong_level_count_or_duplicate_levels_make_the_kpi_incomplete_so_it_is_retried() -> None:
    assert _validate(_compact("K", levels=["x"] * 10)) is None
    assert _validate(_compact("K", levels=["same"] * 11)) is None
    assert _validate(_compact("K", levels=[f"l{i}" for i in range(10)] + [""])) is None


def test_legacy_guidelines_shape_is_still_accepted() -> None:
    legacy = {
        "name": "K", "suggested_weight": 3,
        "guidelines": {str(i): {"qualitative_text": f"t{i}", "quantitative_criteria": None} for i in range(11)},
    }
    assert len(_validate(legacy)["guidelines"]) == 11


def _spec(n: int) -> sb.UserSpec:
    kpis = [
        {"name": f"Signal {i}", "parent_name": None, "level": 1, "weight": None,
         "included_in_scoring": True, "guidance": "", "guidelines": {}}
        for i in range(n)
    ]
    return sb.UserSpec(mode="user_specified", kpis=kpis)


def _fill_fn(delay: float = 0.0, stats: dict | None = None):
    lock = threading.Lock()

    def converse(*, system, tools, **_kw):
        wanted = re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE)
        if stats is not None:
            with lock:
                stats["calls"] = stats.get("calls", 0) + 1
                stats["live"] = stats.get("live", 0) + 1
                stats["peak"] = max(stats.get("peak", 0), stats["live"])
        time.sleep(delay)
        if stats is not None:
            with lock:
                stats["live"] -= 1
        return tool_use_result("fill_user_kpi_details", {"kpis": [_compact(n) for n in wanted]})

    return converse


@pytest.mark.parametrize("n", [35, 100])
async def test_call_count_and_waves_stay_bounded_as_kpi_count_grows(n: int) -> None:
    stats: dict = {}
    progress: list[int] = []
    details, _f, fallback = await sb._enrich_user_kpis(
        _spec(n), FakeBedrockClient(converse_fn=_fill_fn(0.05, stats)), None, None,
        session_id=str(uuid.uuid4()), turn_started_at=None, conversation_context="ctx", jev_client=None,
        findings=[], on_progress=lambda d: progress.append(len(d)),
    )
    settings = get_settings()
    assert len(details) == n and fallback == []
    assert all(len(d["guidelines"]) == 11 for d in details.values())
    assert stats["calls"] == -(-n // settings.user_kpis_per_fill_call)
    assert stats["peak"] <= settings.bedrock_max_concurrency
    assert -(-stats["calls"] // settings.bedrock_max_concurrency) <= 3  # 100 KPIs => at most 3 waves
    assert progress == sorted(progress) and progress[-1] == n and len(progress) == stats["calls"]


async def test_weighting_call_does_not_queue_behind_the_guideline_fan_out() -> None:
    spec = _spec(60)  # 12 chunks at 5/call, more than the 8 Bedrock slots
    weighting_seen = threading.Event()
    inner = _fill_fn()

    def converse(*, system, tools, **kw):
        if "propose_weighting" in {t.name for t in tools or []}:
            weighting_seen.set()
            return tool_use_result(
                "propose_weighting",
                {"approach": "a", "weights": [
                    {"name": k["name"], "relative_importance": 5, "rationale": "r"} for k in spec.kpis
                ]},
            )
        assert weighting_seen.wait(timeout=10), "weighting was stuck behind the guideline calls"
        return inner(system=system, tools=tools, **kw)

    pinned = await sb._prepare_pinned_kpis(
        spec, FakeBedrockClient(converse_fn=converse), None, None,
        session_id=str(uuid.uuid4()), turn_started_at=None, conversation_context="ctx", jev_client=None,
    )
    assert all(len(k["guidelines"]) == 11 for k in pinned.kpis)
    assert not any("Auto-generated fallback" in k["guidelines"]["5"]["qualitative_text"] for k in pinned.kpis)
    assert sum(k["weight"] for k in pinned.kpis) == pytest.approx(100.0, abs=0.05)


async def test_a_timed_out_compact_chunk_is_split_and_recovers() -> None:
    tried: set[frozenset[str]] = set()
    inner = _fill_fn()

    def converse(*, system, tools, **kw):
        wanted = frozenset(re.findall(r'^- "([^"]+)"', system or "", re.MULTILINE))
        if len(wanted) >= 5 and wanted not in tried:
            tried.add(wanted)
            raise BedrockTimeoutError("slow")
        return inner(system=system, tools=tools, **kw)

    details, _f, fallback = await sb._enrich_user_kpis(
        _spec(10), FakeBedrockClient(converse_fn=converse), None, None,
        session_id=str(uuid.uuid4()), turn_started_at=None, conversation_context="ctx", jev_client=None,
        findings=[],
    )
    assert len(details) == 10 and fallback == []


@pytest.mark.usefixtures("_migrated_db")
async def test_the_live_preview_draft_fills_in_progressively_while_guidelines_are_written(monkeypatch) -> None:
    names = [f"Signal {i}" for i in range(12)]
    message = "Support scorecard. KPIs:\n" + "\n".join(f"- {n}" for n in names)
    published: list[int] = []
    real = sb._publish_interim

    def spy(session_id, draft):
        published.append(sum(1 for k in draft["kpis"] if k["guidelines"]))
        real(session_id, draft)

    monkeypatch.setattr(sb, "_publish_interim", spy)
    inner = _fill_fn()

    def converse(*, system, tools, **kw):
        tn = {t.name for t in tools or []}
        if "propose_weighting" in tn:
            return tool_use_result(
                "propose_weighting",
                {"approach": "a", "weights": [{"name": n, "relative_importance": 5, "rationale": "r"} for n in names]},
            )
        if "fill_user_kpi_details" in tn:
            return inner(system=system, tools=tools, **kw)
        return tool_use_result("update_draft", {"patch": {}, "confirmed": True, "assistant_message": "ok"})

    await sb.start_session(str(uuid.uuid4()), message, FakeBedrockClient(converse_fn=converse), web_search_client=None)
    assert published[0] == 0 and 12 in published
    assert any(0 < c < 12 for c in published)  # at least one intermediate state (3 chunks of 5/5/2)
