"""Regression tests for `_normalize_guideline_value`/`_normalize_kpi_guidelines` in
`app/ai/scorecard_builder.py` — a defensive repair for a real, observed GLM-5 tool-call
adherence gap: despite `PROPOSE_KPI_BATCH_TOOL`/`UPDATE_DRAFT_TOOL`'s schema requiring
each guideline rung's value to be `{qualitative_text, quantitative_criteria}`, the model
sometimes returns the rung's value as a bare string instead.

Confirmed live on 2026-09-30 (during this task's Jev-quality-gate live-verification pass,
unrelated to the quality-gate feature itself): a real `propose_kpi_batch` call from
GLM-5 returned exactly this shape for 3 whole KPI candidates in one batch, all of which
`_validate_kpi_batch_items` silently dropped before this fix (logged as "dropping an
invalid KPI item", losing real, usable research content over a nesting mistake alone).
"""

from __future__ import annotations

import uuid

import pytest

from app.ai import scorecard_builder as sb
from tests.fakes import FakeBedrockClient, FakeWebSearchClient, tool_use_result

pytestmark = pytest.mark.usefixtures("_migrated_db")


def _bare_string_guidelines(base: str) -> dict[str, str]:
    """Mirrors the exact malformed shape observed live: every rung's value is a plain
    string, not a `{qualitative_text, quantitative_criteria}` dict."""
    return {str(lvl): f"{base} — level {lvl}." for lvl in range(11)}


# --- Pure-function unit tests ----------------------------------------------------------


def test_normalize_guideline_value_wraps_a_bare_string() -> None:
    assert sb._normalize_guideline_value("Some qualitative text.") == {
        "qualitative_text": "Some qualitative text.",
        "quantitative_criteria": None,
    }


def test_normalize_guideline_value_leaves_a_well_formed_dict_untouched() -> None:
    well_formed = {"qualitative_text": "Good.", "quantitative_criteria": {"metric": "x"}}
    assert sb._normalize_guideline_value(well_formed) == well_formed


def test_normalize_kpi_guidelines_repairs_every_bare_string_rung() -> None:
    candidate = {"name": "Some KPI", "weight": 50, "guidelines": _bare_string_guidelines("Desc")}
    normalized = sb._normalize_kpi_guidelines(candidate)
    assert all(isinstance(v, dict) and "qualitative_text" in v for v in normalized["guidelines"].values())
    assert normalized["guidelines"]["0"]["qualitative_text"] == "Desc — level 0."


def test_normalize_kpi_guidelines_is_a_noop_without_a_guidelines_dict() -> None:
    candidate = {"name": "Some KPI", "weight": 50}
    assert sb._normalize_kpi_guidelines(candidate) == candidate


def test_validate_kpi_batch_items_recovers_bare_string_guidelines_instead_of_dropping() -> None:
    """THE fix, exercised directly against `_validate_kpi_batch_items` (the function the
    real live failure was observed in) — the exact malformed shape from the live log must
    now be RECOVERED, not silently dropped."""
    raw_items = [
        {
            "name": "Substantive Review Comments Per Pull Request",
            "weight": 35,
            "guidelines": _bare_string_guidelines("No review performed"),
        }
    ]
    validated = sb._validate_kpi_batch_items(raw_items, "Test Category")
    assert len(validated) == 1
    assert validated[0]["name"] == "Substantive Review Comments Per Pull Request"
    assert validated[0]["level"] == 2
    assert validated[0]["parent_name"] == "Test Category"
    assert len(validated[0]["guidelines"]) == 11
    assert validated[0]["guidelines"]["0"]["qualitative_text"].startswith("No review performed")


def test_validate_kpi_batch_items_still_drops_a_genuinely_invalid_item() -> None:
    """The fix must NOT paper over a REAL validation failure (e.g. a weight out of the
    0-100 range) — only the specific bare-string-guideline shape is repaired."""
    raw_items = [{"name": "Bad Weight KPI", "weight": 250, "guidelines": _bare_string_guidelines("x")}]
    assert sb._validate_kpi_batch_items(raw_items, "Test Category") == []


# --- End-to-end: the same repair inside a real research-agent batch call ----------------


async def test_research_agent_batch_with_bare_string_guidelines_is_recovered_end_to_end() -> None:
    """Reproduces the real live scenario end-to-end: a research agent's `propose_kpi_batch`
    tool call returns KPIs with bare-string guidelines (exactly as GLM-5 did live) — the
    KPI must now survive into the final merged draft instead of being silently dropped."""
    session_id = str(uuid.uuid4())
    category = {"name": "Test Category", "focus": "Test focus"}

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        names = {t.name for t in (tools or [])}
        if "decide_categories" in names:
            return tool_use_result("decide_categories", {"categories": [category]})
        if "assess_research_coverage" in names:
            return tool_use_result(
                "assess_research_coverage", {"sufficient": True, "reasoning": "ok", "next_categories": []}
            )
        if "record_research_finding" in names:
            return tool_use_result(
                "record_research_finding",
                {"summary": "A finding.", "suggested_kpis": [], "suggested_thresholds": [], "sources": []},
            )
        if "propose_kpi_batch" in names:
            return tool_use_result(
                "propose_kpi_batch",
                {
                    "kpis": [
                        {
                            "name": "Malformed-Shape KPI",
                            "weight": 100,
                            "guidelines": _bare_string_guidelines("Bare string rung"),
                        }
                    ]
                },
            )
        return tool_use_result(
            "update_draft",
            {
                "patch": {
                    "name": "Test Scorecard",
                    "purpose": "Purpose.",
                    "domain": "Domain",
                    "audience": "Audience",
                    "target_score": 8,
                },
                "confirmed": True,
            },
        )

    fake_bedrock = FakeBedrockClient(converse_fn=converse_fn)
    fake_search = FakeWebSearchClient(search_fn=lambda q: [])

    turn = await sb.start_session(
        session_id, "Build me a test scorecard.", fake_bedrock, web_search_client=fake_search
    )

    assert turn.status == "confirmed"
    kpi_names = [k["name"] for k in turn.draft["kpis"]]
    assert "Malformed-Shape KPI" in kpi_names, (
        "the bare-string-guideline KPI should have been recovered, not silently dropped"
    )


# --- End-to-end: the same repair inside an update_draft patch ---------------------------


async def test_update_draft_patch_with_bare_string_guidelines_is_recovered_not_rejected() -> None:
    """The identical shape gap can occur in a direct `update_draft` patch too — it must be
    repaired before validation rather than REJECTING an otherwise-good patch."""
    session_id = str(uuid.uuid4())
    fake_bedrock = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {
                    "patch": {
                        "name": "Test Scorecard",
                        "purpose": "Purpose.",
                        "domain": "Domain",
                        "audience": "Audience",
                        "target_score": 8,
                        "kpis": [
                            {
                                "name": "Bare String Guideline KPI",
                                "weight": 100,
                                "level": 1,
                                "guidelines": _bare_string_guidelines("Bare string rung"),
                            }
                        ],
                    },
                    "confirmed": True,
                },
            )
        ]
    )

    turn = await sb.start_session(session_id, "Build me a test scorecard.", fake_bedrock)

    assert turn.status == "confirmed"
    assert turn.draft["kpis"][0]["name"] == "Bare String Guideline KPI"
    assert turn.draft["kpis"][0]["guidelines"]["0"]["qualitative_text"].startswith("Bare string rung")
