"""Smoke tests for the Cycle 1b CRUD API, against a real Postgres DB via FastAPI's
TestClient (no mocks)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_me_profile_and_removed_user_admin_api(client: TestClient, seed_user_id: str) -> None:
    h = {"X-User-Id": seed_user_id}
    me = client.get("/api/v1/me", headers=h)
    assert me.status_code == 200
    assert me.json()["email"] == "bootstrap@qualityscorecard.local"
    assert "password_hash" not in me.json()

    r = client.patch("/api/v1/me", json={"name": "Renamed", "role": "admin"}, headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"
    assert r.json()["role"] == "user"  # a user cannot promote themselves

    # The open user-admin endpoints are gone.
    assert client.get(f"/api/v1/users/{seed_user_id}", headers=h).status_code == 404
    assert client.get("/api/v1/me", headers={"X-Test-Anonymous": "1"}).status_code == 401


def test_scorecard_version_kpi_bulk_create_and_nested_read(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "api-flow@example.com", "name": "API Flow"},
        headers={"X-User-Id": seed_user_id},
    ).json()

    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": "API Flow Scorecard", "owner_id": user["id"]},
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
                "name": "KPI A",
                "weight": 60,
                "display_order": 0,
                "guidelines": [{"score_level": 10, "qualitative_text": "Excellent"}],
            },
            {"parent_id": None, "level": 1, "name": "KPI B", "weight": 40, "display_order": 1},
        ]
    }
    r = client.post(
        f"/api/v1/scorecard-versions/{version['id']}/kpi-nodes/bulk",
        json=bulk_payload,
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 201, r.text
    assert len(r.json()) == 2

    r = client.get(f"/api/v1/scorecards/{scorecard['id']}/versions/{version['id']}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["kpi_nodes"]) == 2
    assert any(n["guidelines"] for n in body["kpi_nodes"])


def test_kpi_bulk_create_rejects_unbalanced_weights(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "api-unbalanced@example.com", "name": "API Unbalanced"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": "Unbalanced SC", "owner_id": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()

    bad_payload = {"nodes": [{"parent_id": None, "level": 1, "name": "Solo KPI", "weight": 50}]}
    r = client.post(
        f"/api/v1/scorecard-versions/{version['id']}/kpi-nodes/bulk",
        json=bad_payload,
        headers={"X-User-Id": user["id"]},
    )
    assert r.status_code == 409
    assert "weight" in r.text.lower() or "sum" in r.text.lower()
