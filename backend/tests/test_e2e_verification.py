"""Real end-to-end verification tests for the core "Quality Scorecard System" user
journey, requested explicitly by the project owner (2026-09-28 verification pass).

Covers two of the four verification sections against the real Postgres test database
(never mocks — see tests/conftest.py's module docstring) with the chat builder's real
LangGraph state machine driven through a scripted `FakeBedrockClient`:

1. A realistic multi-turn chat conversation genuinely EDITS the in-progress draft (a
   weight change on an already-proposed KPI) before confirmation, and that edited state
   -- not the original proposal -- is what survives into the materialized Scorecard saved
   under an explicit custom name (`test_chat_iteration_edits_survive_to_saved_scorecard`).
2. An already-saved (published) scorecard can genuinely be changed post-save, via both
   realistic paths:
   - a direct PATCH/bulk-update on `kpi_nodes` for the current version
     (`test_direct_kpi_edit_on_published_scorecard_persists`)
   - a version-bump (new `scorecard_versions` row + `current_version_id` repoint) that
     preserves the OLD version and any evaluations recorded against it
     (`test_version_bump_preserves_old_version_and_its_evaluations`) -- this is also the
     Cycle-1 scenario catalogue's own "migration case" (version bump preserving
     evaluations), re-confirmed here after later changes to the app.

Section 3 (manual "fill values from UI" evaluation flow) and section 4 (frontend
simplicity read-through) were verified separately, directly against the live dev stack
and by reading the frontend source respectively -- see the verification report; they are
not part of this automated file because section 3 specifically needs a *seeded* scorecard
with a full 11-level guideline set (seed data lives in the dev DB, not this disposable
per-test-truncated test DB) and section 4 involves no backend calls at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.deps import get_bedrock_client
from app.main import app
from tests.fakes import FakeBedrockClient, tool_use_result

# --- Section 1: chat iteration genuinely updates the draft before it is saved ----------

_INITIAL_KPIS = [
    {
        "name": "Accuracy",
        "weight": 25,
        "level": 1,
        "parent_name": None,
        "guidelines": {
            "10": {"qualitative_text": "Fully accurate uptime data, no discrepancies."},
            "0": {"qualitative_text": "Completely inaccurate uptime data."},
        },
    },
    {
        "name": "Timeliness",
        "weight": 25,
        "level": 1,
        "parent_name": None,
        "guidelines": {
            "10": {"qualitative_text": "Published within 5 minutes of the incident."},
            "0": {"qualitative_text": "Never published or published days late."},
        },
    },
    {
        "name": "Clarity",
        "weight": 20,
        "level": 1,
        "parent_name": None,
        "guidelines": {
            "10": {"qualitative_text": "Unambiguous, plain-language status."},
            "0": {"qualitative_text": "Incomprehensible jargon."},
        },
    },
    {
        "name": "Professionalism",
        "weight": 15,
        "level": 1,
        "parent_name": None,
        "guidelines": {
            "10": {"qualitative_text": "Consistently professional tone."},
            "0": {"qualitative_text": "Unprofessional tone."},
        },
    },
    {
        "name": "Completeness",
        "weight": 15,
        "level": 1,
        "parent_name": None,
        "guidelines": {
            "10": {"qualitative_text": "Covers impact, cause, ETA and next update."},
            "0": {"qualitative_text": "Missing all required fields."},
        },
    },
]

# The user's mid-conversation edit: Timeliness 25 -> 40, Accuracy 25 -> 10 (siblings still
# sum to 100). Everything else is untouched -- this is the "genuine edit" the test proves
# survives into the saved chart.
_EDITED_KPIS = [
    {**kpi, "weight": 10} if kpi["name"] == "Accuracy" else kpi for kpi in _INITIAL_KPIS
]
_EDITED_KPIS = [
    {**kpi, "weight": 40} if kpi["name"] == "Timeliness" else kpi for kpi in _EDITED_KPIS
]

_CUSTOM_NAME = "E2E Chat Iteration Test Scorecard"


def _by_name(kpis: list[dict], name: str) -> dict:
    return next(k for k in kpis if k["name"] == name)


def test_chat_iteration_edits_survive_to_saved_scorecard(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "chat-iteration@example.com", "name": "Chat Iteration User"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    # --- Turn 1: user describes a brand-new scorecard need (no seeded scorecards exist in
    # this disposable test DB at all, so check_similarity is guaranteed to find nothing --
    # no "start fresh" detour needed). The model proposes 5 KPIs with weights via
    # update_draft (LLM-first), then, since it's not confirming yet, loops back for one
    # more turn and asks whether to adjust anything before saving.
    turn1_bedrock = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {
                    "patch": {
                        "name": "Draft Uptime Report Scorecard",
                        "purpose": "Rate the quality of internal tooling uptime status reports.",
                        "domain": "Internal Tooling Uptime Reports",
                        "audience": "SRE leads",
                        "target_score": 8,
                        "kpis": _INITIAL_KPIS,
                    },
                    "confirmed": False,
                },
            ),
            tool_use_result(
                "ask_clarification",
                {
                    "question": "Here's a first draft -- want to change anything before we save it?",
                    "options": [],
                    "missing_fields": [],
                },
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: turn1_bedrock
    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "I need a scorecard to rate internal tooling uptime status reports."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "awaiting_clarification"
    session_id = body["session_id"]
    proposed_kpis = body["draft"]["kpis"]
    assert [k["name"] for k in proposed_kpis] == [
        "Accuracy", "Timeliness", "Clarity", "Professionalism", "Completeness",
    ]
    assert _by_name(proposed_kpis, "Timeliness")["weight"] == 25
    assert _by_name(proposed_kpis, "Accuracy")["weight"] == 25

    # --- Turn 2: the user explicitly asks for a CHANGE (not the final confirm). The model
    # applies the requested weight edit via update_draft (still not confirmed), then loops
    # back once more and asks whether to save.
    turn2_bedrock = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {"patch": {"kpis": _EDITED_KPIS}, "confirmed": False},
            ),
            tool_use_result(
                "ask_clarification",
                {
                    "question": "Updated. Anything else, or should I save this?",
                    "options": ["Save it", "Change something else"],
                    "missing_fields": [],
                },
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: turn2_bedrock
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "Change the weight of Timeliness to 40% and Accuracy to 10%."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # This is the core assertion for "iteration genuinely updates the draft": the
    # IN-PROGRESS draft (status still awaiting_clarification, i.e. NOT yet confirmed or
    # saved) already reflects the edit.
    assert body["status"] == "awaiting_clarification"
    edited_kpis = body["draft"]["kpis"]
    assert _by_name(edited_kpis, "Timeliness")["weight"] == 40
    assert _by_name(edited_kpis, "Accuracy")["weight"] == 10
    # Untouched siblings are exactly as originally proposed.
    assert _by_name(edited_kpis, "Clarity")["weight"] == 20
    assert _by_name(edited_kpis, "Professionalism")["weight"] == 15
    assert _by_name(edited_kpis, "Completeness")["weight"] == 15

    # Cross-check against the session's own state endpoint (GET, no graph advance) --
    # proves the edit is durable in checkpointed state, not just in this one response.
    r = client.get(f"/api/v1/chat/sessions/{session_id}")
    assert r.status_code == 200
    state_kpis = r.json()["draft"]["kpis"]
    assert _by_name(state_kpis, "Timeliness")["weight"] == 40
    assert _by_name(state_kpis, "Accuracy")["weight"] == 10

    # --- Turn 3: the user confirms, giving an explicit CUSTOM NAME. The model renames the
    # draft and sets confirmed=True; nothing about the KPI list is touched, so materialization
    # must carry forward the turn-2 edit, not the turn-1 original.
    turn3_bedrock = FakeBedrockClient(
        script=[
            tool_use_result(
                "update_draft",
                {"patch": {"name": _CUSTOM_NAME}, "confirmed": True},
            ),
        ]
    )
    app.dependency_overrides[get_bedrock_client] = lambda: turn3_bedrock
    r = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": f"Save this as '{_CUSTOM_NAME}'."},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    scorecard_id = body["materialized_scorecard_id"]
    version_id = body["materialized_scorecard_version_id"]
    assert scorecard_id is not None
    assert version_id is not None

    app.dependency_overrides.pop(get_bedrock_client, None)

    # --- Real-DB proof: a scorecard now exists under EXACTLY the custom name, owned by
    # the right user, with the EDITED (not original) weights.
    r = client.get(f"/api/v1/scorecards/{scorecard_id}")
    assert r.status_code == 200
    scorecard = r.json()
    assert scorecard["name"] == _CUSTOM_NAME
    assert scorecard["owner_id"] == user["id"]
    assert scorecard["current_version_id"] == version_id

    r = client.get(f"/api/v1/scorecards/{scorecard_id}/versions/{version_id}")
    assert r.status_code == 200
    nodes = r.json()["kpi_nodes"]
    weights_by_name = {n["name"]: float(n["weight"]) for n in nodes}
    assert weights_by_name == {
        "Accuracy": 10.0,
        "Timeliness": 40.0,
        "Clarity": 20.0,
        "Professionalism": 15.0,
        "Completeness": 15.0,
    }
    assert sum(weights_by_name.values()) == 100.0

    # Also visible/findable via the plain scorecards list, under the exact custom name.
    r = client.get("/api/v1/scorecards", params={"owner_id": user["id"]})
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert _CUSTOM_NAME in names
    assert "Draft Uptime Report Scorecard" not in names  # the pre-rename temp name is gone


# --- Section 2a: direct API edit of an already-saved (published) scorecard's KPIs -------


def test_direct_kpi_edit_on_published_scorecard_persists(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "direct-edit@example.com", "name": "Direct Edit User"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": "Direct-Edit Test Scorecard", "owner_id": user["id"], "status": "draft"},
        headers={"X-User-Id": user["id"]},
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    bulk = client.post(
        f"/api/v1/scorecard-versions/{version['id']}/kpi-nodes/bulk",
        json={
            "nodes": [
                {
                    "parent_id": None, "level": 1, "name": "Speed", "weight": 60, "display_order": 0,
                    "guidelines": [{"score_level": 10, "qualitative_text": "Fast."}],
                },
                {
                    "parent_id": None, "level": 1, "name": "Correctness", "weight": 40, "display_order": 1,
                    "guidelines": [{"score_level": 10, "qualitative_text": "Correct."}],
                },
            ]
        },
        headers={"X-User-Id": user["id"]},
    ).json()
    speed_id = next(n["id"] for n in bulk if n["name"] == "Speed")
    correctness_id = next(n["id"] for n in bulk if n["name"] == "Correctness")

    # "Published" -- this is a real saved chart, not a draft-in-progress.
    r = client.patch(
        f"/api/v1/scorecards/{scorecard['id']}",
        json={"status": "published", "current_version_id": version["id"]},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"

    # --- Direct edit path 1: bulk weight rebalance (60/40 -> 70/30).
    r = client.patch(
        "/api/v1/kpi-nodes/weights",
        json={"weights": [{"id": speed_id, "weight": 70}, {"id": correctness_id, "weight": 30}]},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    weights = {n["id"]: float(n["weight"]) for n in r.json()}
    assert weights[speed_id] == 70.0
    assert weights[correctness_id] == 30.0

    # --- Direct edit path 2: rename a KPI.
    r = client.patch(
        f"/api/v1/kpi-nodes/{speed_id}",
        json={"name": "Turnaround Speed"},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Turnaround Speed"

    # --- Persistence check: re-fetch the version fresh and confirm both edits stuck.
    r = client.get(f"/api/v1/scorecards/{scorecard['id']}/versions/{version['id']}")
    assert r.status_code == 200
    nodes = r.json()["kpi_nodes"]
    by_id = {n["id"]: n for n in nodes}
    assert by_id[speed_id]["name"] == "Turnaround Speed"
    assert float(by_id[speed_id]["weight"]) == 70.0
    assert float(by_id[correctness_id]["weight"]) == 30.0
    assert float(by_id[speed_id]["weight"]) + float(by_id[correctness_id]["weight"]) == 100.0

    # --- The weight-sum trigger still holds after all this: an update that breaks the
    # sibling sum (70/30 -> 70/actually-still-30, but only patch one side to 50, leaving
    # the group at 70+50=120) must be rejected with 409, not silently accepted.
    r = client.patch(
        "/api/v1/kpi-nodes/weights",
        json={"weights": [{"id": correctness_id, "weight": 50}]},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 409, r.text

    # And the rejected change did not partially apply.
    r = client.get(f"/api/v1/kpi-nodes/{correctness_id}")
    assert float(r.json()["weight"]) == 30.0


# --- Section 2b: version-bump edit preserving the old version + its evaluations ---------


def test_version_bump_preserves_old_version_and_its_evaluations(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "version-bump@example.com", "name": "Version Bump User"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": "Version-Bump Test Scorecard", "owner_id": user["id"], "status": "published"},
        headers={"X-User-Id": user["id"]},
    ).json()
    v1 = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    v1_nodes = client.post(
        f"/api/v1/scorecard-versions/{v1['id']}/kpi-nodes/bulk",
        json={
            "nodes": [
                {
                    "parent_id": None, "level": 1, "name": "Quality", "weight": 100, "display_order": 0,
                    "guidelines": [
                        {"score_level": 10, "qualitative_text": "Excellent."},
                        {"score_level": 0, "qualitative_text": "Unacceptable."},
                    ],
                },
            ]
        },
        headers={"X-User-Id": user["id"]},
    ).json()
    quality_v1_id = v1_nodes[0]["id"]

    client.patch(
        f"/api/v1/scorecards/{scorecard['id']}",
        json={"current_version_id": v1["id"]},
        headers={"X-User-Id": user["id"]},
    )

    # An evaluation recorded against v1, BEFORE the version bump.
    evaluation = client.post(
        "/api/v1/evaluations",
        json={"scorecard_version_id": v1["id"], "name": "Pre-Bump Evaluation", "evaluated_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    kpi_result = client.post(
        f"/api/v1/evaluations/{evaluation['id']}/results",
        json={"kpi_node_id": quality_v1_id, "score": 8.5, "matched_guideline_level": 8, "reasoning_text": "Solid."},
        headers={"X-User-Id": user["id"]},
    ).json()
    client.patch(
        f"/api/v1/evaluations/{evaluation['id']}",
        json={"status": "completed", "final_weighted_score": 8.5, "rag_band": "band_8"},
        headers={"X-User-Id": user["id"]},
    )

    # --- "Refine with assistant" produces a new version with a modified KPI set (here:
    # the single KPI split into two, mirroring a real refinement).
    v2 = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 2, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    v2_nodes = client.post(
        f"/api/v1/scorecard-versions/{v2['id']}/kpi-nodes/bulk",
        json={
            "nodes": [
                {
                    "parent_id": None, "level": 1, "name": "Correctness", "weight": 60, "display_order": 0,
                    "guidelines": [{"score_level": 10, "qualitative_text": "Correct."}],
                },
                {
                    "parent_id": None, "level": 1, "name": "Presentation", "weight": 40, "display_order": 1,
                    "guidelines": [{"score_level": 10, "qualitative_text": "Well presented."}],
                },
            ]
        },
        headers={"X-User-Id": user["id"]},
    ).json()
    assert len(v2_nodes) == 2

    # Repoint the scorecard's current_version_id at the new version.
    r = client.patch(
        f"/api/v1/scorecards/{scorecard['id']}",
        json={"current_version_id": v2["id"]},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_version_id"] == v2["id"]

    # --- Migration-case assertions: the OLD version is still fully readable...
    r = client.get(f"/api/v1/scorecards/{scorecard['id']}/versions/{v1['id']}")
    assert r.status_code == 200, r.text
    v1_readback = r.json()
    assert len(v1_readback["kpi_nodes"]) == 1
    assert v1_readback["kpi_nodes"][0]["name"] == "Quality"
    assert len(v1_readback["kpi_nodes"][0]["guidelines"]) == 2

    # ...and the evaluation recorded against it is STILL intact/readable, still pointing
    # at v1 (not silently migrated/rewritten to point at v2), with its original score.
    r = client.get(f"/api/v1/evaluations/{evaluation['id']}")
    assert r.status_code == 200, r.text
    eval_readback = r.json()
    assert eval_readback["scorecard_version_id"] == v1["id"]
    assert eval_readback["status"] == "completed"
    assert float(eval_readback["final_weighted_score"]) == 8.5
    assert eval_readback["rag_band"] == "band_8"
    assert len(eval_readback["kpi_results"]) == 1
    assert eval_readback["kpi_results"][0]["id"] == kpi_result["id"]
    assert float(eval_readback["kpi_results"][0]["score"]) == 8.5
    assert eval_readback["kpi_results"][0]["reasoning_text"] == "Solid."

    # And the referential-integrity guarantee behind "preserved" is real, not just
    # untouched by coincidence: v1 cannot be deleted while an evaluation still references
    # it (ondelete="RESTRICT" on evaluations.scorecard_version_id).
    r = client.delete(
        f"/api/v1/scorecards/{scorecard['id']}/versions/{v1['id']}",
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 409, r.text

    # v1 is still there and unaffected by the failed delete attempt.
    r = client.get(f"/api/v1/scorecards/{scorecard['id']}/versions/{v1['id']}")
    assert r.status_code == 200
