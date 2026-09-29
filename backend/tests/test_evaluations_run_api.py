"""End-to-end test of POST /evaluations/{id}/run through the real FastAPI app + real
Postgres, with get_bedrock_client overridden to a FakeBedrockClient (see tests/fakes.py —
no real Bedrock credentials in this environment)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.deps import get_bedrock_client
from app.main import app
from tests.fakes import FakeBedrockClient, text_result, tool_use_result


def test_run_evaluation_persists_weighted_score_and_results(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "eval-run-api@example.com", "name": "Eval Run"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": "Eval Run Scorecard", "owner_id": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    bulk_payload = {
        "nodes": [
            {
                "parent_id": None,
                "level": 1,
                "name": "Accuracy",
                "weight": 100,
                "display_order": 0,
                "guidelines": [{"score_level": 10, "qualitative_text": "Perfect."}],
            },
        ]
    }
    client.post(
        f"/api/v1/scorecard-versions/{version['id']}/kpi-nodes/bulk",
        json=bulk_payload,
        headers={"X-User-Id": user["id"]},
    )

    evaluation = client.post(
        "/api/v1/evaluations",
        json={"scorecard_version_id": version["id"], "name": "Run 1", "evaluated_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()

    def converse_fn(*, messages, system, tools, force_tool_use, model_id):
        if not tools:
            return text_result("Evidence: response was accurate.")
        return tool_use_result(
            "record_kpi_judgment",
            {
                "matched_level": 10,
                "evidence_quotes": ["response was accurate"],
                "reasoning": "Matches level 10.",
                "score": 10,
            },
        )

    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=converse_fn)
    try:
        r = client.post(
            f"/api/v1/evaluations/{evaluation['id']}/run",
            json={"input_text": "The response was accurate."},
            headers={"X-User-Id": user["id"]},
        )
    finally:
        app.dependency_overrides.pop(get_bedrock_client, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert float(body["final_weighted_score"]) == 10.0
    assert body["rag_band"] == "band_10_9"
    assert len(body["kpi_results"]) == 1
    assert body["kpi_results"][0]["matched_guideline_level"] == 10
