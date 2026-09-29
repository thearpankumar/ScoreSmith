"""End-to-end tests for `POST /evaluations/{id}/finalize` (Part 2b's manual-evaluation
finalize endpoint — see app/api/v1/evaluations.py), through the real FastAPI app + real
Postgres. No Bedrock involved (manual scoring path).

Covers, per this pass's task notes:
- the default weighted-average formula (scoring_formula IS NULL) still computes correctly
  with REAL Decimal-typed scores read back from Postgres (a real bug found via live
  testing: `EvaluationKpiResult.score` is a Numeric column, so `simpleeval`'s arithmetic —
  and even plain `Decimal * float` — raises unless coerced to float first; see the
  `float(r.score)` coercion in `finalize_evaluation`);
- a non-trivial custom `scoring_formula` (min/max, non-uniform weighting) computes the
  exact hand-computable expected value;
- a KPI with `included_in_scoring=False` is excluded from the weight-sum constraint and
  from the default formula's computation, but its score is still recorded.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _make_scorecard_and_version(client: TestClient, user_id: str, name: str) -> tuple[str, str]:
    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": name, "owner_id": user_id},
        headers={"X-User-Id": user_id},
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user_id},
        headers={"X-User-Id": user_id},
    ).json()
    return scorecard["id"], version["id"]


def test_finalize_default_formula_with_real_decimal_scores(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "finalize-default@example.com", "name": "Finalize Default"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard_id, version_id = _make_scorecard_and_version(client, user["id"], "Finalize Default Scorecard")

    bulk_payload = {
        "nodes": [
            {
                "parent_id": None, "level": 1, "name": "A", "weight": 60, "display_order": 0,
                "guidelines": [{"score_level": 8, "qualitative_text": "Good."}],
            },
            {
                "parent_id": None, "level": 1, "name": "B", "weight": 40, "display_order": 1,
                "guidelines": [{"score_level": 4, "qualitative_text": "OK."}],
            },
        ]
    }
    nodes = client.post(
        f"/api/v1/scorecard-versions/{version_id}/kpi-nodes/bulk",
        json=bulk_payload,
        headers={"X-User-Id": user["id"]},
    ).json()
    node_a = next(n for n in nodes if n["name"] == "A")
    node_b = next(n for n in nodes if n["name"] == "B")

    evaluation = client.post(
        "/api/v1/evaluations",
        json={
            "scorecard_version_id": version_id, "name": "Manual eval", "evaluated_by": user["id"],
            "status": "in_progress",
        },
        headers={"X-User-Id": user["id"]},
    ).json()

    for node, score in [(node_a, 8), (node_b, 4)]:
        r = client.post(
            f"/api/v1/evaluations/{evaluation['id']}/results",
            json={"kpi_node_id": node["id"], "score": score, "matched_guideline_level": score},
            headers={"X-User-Id": user["id"]},
        )
        assert r.status_code == 201, r.text

    r = client.post(f"/api/v1/evaluations/{evaluation['id']}/finalize", headers={"X-User-Id": user["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    # 0.6*8 + 0.4*4 = 4.8 + 1.6 = 6.4 — the classic weighted average, unchanged.
    assert float(body["final_weighted_score"]) == 6.4
    assert len(body["kpi_results"]) == 2


def test_finalize_custom_formula_matches_hand_computed_value(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "finalize-formula@example.com", "name": "Finalize Formula"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard_id, version_id = _make_scorecard_and_version(client, user["id"], "Finalize Formula Scorecard")

    bulk_payload = {
        "nodes": [
            {
                "parent_id": None, "level": 1, "name": "Speed", "weight": 50, "display_order": 0,
                "guidelines": [{"score_level": 6, "qualitative_text": "Fine."}],
            },
            {
                "parent_id": None, "level": 1, "name": "Accuracy", "weight": 50, "display_order": 1,
                "guidelines": [{"score_level": 9, "qualitative_text": "Great."}],
            },
        ]
    }
    nodes = client.post(
        f"/api/v1/scorecard-versions/{version_id}/kpi-nodes/bulk",
        json=bulk_payload,
        headers={"X-User-Id": user["id"]},
    ).json()
    node_speed = next(n for n in nodes if n["name"] == "Speed")
    node_accuracy = next(n for n in nodes if n["name"] == "Accuracy")

    # Reject an invalid formula (unknown KPI reference).
    r = client.patch(
        f"/api/v1/scorecards/{scorecard_id}/versions/{version_id}",
        json={"scoring_formula": 'kpi["Nope"] * 2'},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 422, r.text

    # Accept a genuinely non-trivial one (min, non-uniform weighting).
    formula = 'min(kpi["Speed"], kpi["Accuracy"]) * 0.7 + kpi["Accuracy"] * 0.3'
    r = client.patch(
        f"/api/v1/scorecards/{scorecard_id}/versions/{version_id}",
        json={"scoring_formula": formula},
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scoring_formula"] == formula

    # Live validate-formula endpoint too.
    r = client.post(
        f"/api/v1/scorecards/{scorecard_id}/versions/{version_id}/validate-formula",
        json={"formula": formula},
    )
    assert r.status_code == 200
    assert r.json() == {"valid": True, "error": None, "unused_kpis": []}

    evaluation = client.post(
        "/api/v1/evaluations",
        json={
            "scorecard_version_id": version_id, "name": "Formula eval", "evaluated_by": user["id"],
            "status": "in_progress",
        },
        headers={"X-User-Id": user["id"]},
    ).json()
    for node, score in [(node_speed, 6), (node_accuracy, 9)]:
        client.post(
            f"/api/v1/evaluations/{evaluation['id']}/results",
            json={"kpi_node_id": node["id"], "score": score, "matched_guideline_level": score},
            headers={"X-User-Id": user["id"]},
        )

    r = client.post(f"/api/v1/evaluations/{evaluation['id']}/finalize", headers={"X-User-Id": user["id"]})
    assert r.status_code == 200, r.text
    # min(6, 9) * 0.7 + 9 * 0.3 = 6*0.7 + 2.7 = 4.2 + 2.7 = 6.9 — NOT the default weighted
    # average (which would be 0.5*6 + 0.5*9 = 7.5) — proving the custom formula, not the
    # default math, was actually used.
    assert float(r.json()["final_weighted_score"]) == 6.9


def test_included_in_scoring_false_excluded_from_weight_sum_and_default_formula(
    client: TestClient, seed_user_id: str
) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "finalize-excluded@example.com", "name": "Finalize Excluded"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard_id, version_id = _make_scorecard_and_version(client, user["id"], "Finalize Excluded Scorecard")

    # A + B sum to 100 between themselves; C is excluded from scoring entirely (its own
    # weight is irrelevant to the sibling-sum constraint) — the bulk-create trigger check
    # must NOT reject this despite A + B + C (weight) != 100 counting C.
    bulk_payload = {
        "nodes": [
            {
                "parent_id": None, "level": 1, "name": "A", "weight": 70, "display_order": 0,
                "guidelines": [{"score_level": 10, "qualitative_text": "Great."}],
            },
            {
                "parent_id": None, "level": 1, "name": "B", "weight": 30, "display_order": 1,
                "guidelines": [{"score_level": 2, "qualitative_text": "Weak."}],
            },
            {
                "parent_id": None, "level": 1, "name": "C (informational)", "weight": 55,
                "display_order": 2, "included_in_scoring": False,
                "guidelines": [{"score_level": 3, "qualitative_text": "Tracked only."}],
            },
        ]
    }
    r = client.post(
        f"/api/v1/scorecard-versions/{version_id}/kpi-nodes/bulk",
        json=bulk_payload,
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    nodes = r.json()
    node_a = next(n for n in nodes if n["name"] == "A")
    node_b = next(n for n in nodes if n["name"] == "B")
    node_c = next(n for n in nodes if n["name"] == "C (informational)")
    assert node_c["included_in_scoring"] is False

    evaluation = client.post(
        "/api/v1/evaluations",
        json={
            "scorecard_version_id": version_id, "name": "Excluded-KPI eval", "evaluated_by": user["id"],
            "status": "in_progress",
        },
        headers={"X-User-Id": user["id"]},
    ).json()
    for node, score in [(node_a, 10), (node_b, 2), (node_c, 3)]:
        r = client.post(
            f"/api/v1/evaluations/{evaluation['id']}/results",
            json={"kpi_node_id": node["id"], "score": score, "matched_guideline_level": score},
            headers={"X-User-Id": user["id"]},
        )
        assert r.status_code == 201, r.text

    r = client.post(f"/api/v1/evaluations/{evaluation['id']}/finalize", headers={"X-User-Id": user["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    # 0.7*10 + 0.3*2 = 7 + 0.6 = 7.6 — C's score (3) never enters the computation despite
    # being recorded (see kpi_results below).
    assert float(body["final_weighted_score"]) == 7.6
    assert len(body["kpi_results"]) == 3
    c_result = next(kr for kr in body["kpi_results"] if kr["kpi_node_id"] == node_c["id"])
    assert float(c_result["score"]) == 3.0
