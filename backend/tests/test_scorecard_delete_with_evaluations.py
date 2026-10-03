"""Deleting a scorecard that already has evaluations (and their per-KPI results).

Regression for a real bug seen in the UI ("Could not reach the backend ... NetworkError"):
`DELETE /scorecards/{id}` returned a 500 because the ORM tried to set
`evaluation_kpi_results.kpi_node_id` to NULL while deleting the scorecard's KPI nodes, which
the `check_eval_kpi_result_version_match` trigger rejects ("evaluation or kpi_node not found").
The delete dialog promises that the scorecard, "every version of it, and its evaluations" are
deleted, so the evaluations (and their results) must go first.

A 500 also surfaced in the browser as an opaque network error (an unhandled exception skips
the CORS middleware), so database integrity errors must come back as a normal JSON 409.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _scorecard_with_evaluation(client: TestClient, user_id: str) -> tuple[str, str, str, str]:
    headers = {"X-User-Id": user_id}
    scorecard = client.post(
        "/api/v1/scorecards", json={"name": "Delete me", "owner_id": user_id}, headers=headers
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user_id},
        headers=headers,
    ).json()
    nodes = client.post(
        f"/api/v1/scorecard-versions/{version['id']}/kpi-nodes/bulk",
        json={
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
        },
        headers=headers,
    ).json()
    evaluation = client.post(
        "/api/v1/evaluations",
        json={
            "scorecard_version_id": version["id"], "name": "Eval", "evaluated_by": user_id,
            "status": "in_progress",
        },
        headers=headers,
    ).json()
    for node in nodes:
        r = client.post(
            f"/api/v1/evaluations/{evaluation['id']}/results",
            json={"kpi_node_id": node["id"], "score": 7, "matched_guideline_level": 7},
            headers=headers,
        )
        assert r.status_code == 201, r.text
    return scorecard["id"], version["id"], evaluation["id"], nodes[0]["id"]


def test_delete_scorecard_with_evaluations_removes_everything(
    client: TestClient, seed_user_id: str
) -> None:
    scorecard_id, version_id, evaluation_id, node_id = _scorecard_with_evaluation(client, seed_user_id)
    headers = {"X-User-Id": seed_user_id}

    r = client.delete(f"/api/v1/scorecards/{scorecard_id}", headers=headers)
    assert r.status_code == 204, r.text

    assert client.get(f"/api/v1/scorecards/{scorecard_id}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/evaluations/{evaluation_id}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/scorecards/{scorecard_id}/versions", headers=headers).json() == []


def test_delete_scorecard_leaves_other_scorecards_evaluations_alone(
    client: TestClient, seed_user_id: str
) -> None:
    keep_scorecard, _keep_version, keep_evaluation, _ = _scorecard_with_evaluation(client, seed_user_id)
    drop_scorecard, _drop_version, drop_evaluation, _ = _scorecard_with_evaluation(client, seed_user_id)
    headers = {"X-User-Id": seed_user_id}

    assert client.delete(f"/api/v1/scorecards/{drop_scorecard}", headers=headers).status_code == 204

    assert client.get(f"/api/v1/evaluations/{drop_evaluation}", headers=headers).status_code == 404
    kept = client.get(f"/api/v1/evaluations/{keep_evaluation}", headers=headers)
    assert kept.status_code == 200
    assert client.get(f"/api/v1/scorecards/{keep_scorecard}", headers=headers).status_code == 200


def test_delete_version_with_evaluation_is_a_clean_409(client: TestClient, seed_user_id: str) -> None:
    """Deleting just a VERSION that evaluations point at stays blocked (RESTRICT) — as a JSON 409."""
    scorecard_id, version_id, _evaluation_id, _ = _scorecard_with_evaluation(client, seed_user_id)
    r = client.delete(
        f"/api/v1/scorecards/{scorecard_id}/versions/{version_id}", headers={"X-User-Id": seed_user_id}
    )
    assert r.status_code == 409, r.text


def test_deleting_a_kpi_node_that_has_results_is_a_clean_409_not_a_500(
    client: TestClient, seed_user_id: str
) -> None:
    _scorecard_id, _version_id, _evaluation_id, node_id = _scorecard_with_evaluation(client, seed_user_id)
    r = client.delete(f"/api/v1/kpi-nodes/{node_id}", headers={"X-User-Id": seed_user_id})
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/json")


def test_unhandled_integrity_error_is_a_json_409_with_cors_headers(client: TestClient) -> None:
    """The browser only shows a real API error (not "NetworkError") if the response has CORS headers."""
    from sqlalchemy.exc import IntegrityError

    from app.main import app

    @app.get("/__test_integrity_error")
    async def _boom() -> None:
        raise IntegrityError("stmt", {}, Exception("fk violated"))

    try:
        r = client.get("/__test_integrity_error", headers={"Origin": "http://localhost:3000"})
    finally:
        app.router.routes[:] = [r_ for r_ in app.router.routes if getattr(r_, "path", "") != "/__test_integrity_error"]
    assert r.status_code == 409, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "fk violated" in r.json()["detail"]
