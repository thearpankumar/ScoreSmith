"""End-to-end test of POST /scorecards/suggest-similar through the real FastAPI app +
real Postgres/pgvector, with get_bedrock_client overridden to a FakeBedrockClient whose
`.embed()` is deterministic (see tests/fakes.py) — no real Bedrock credentials needed."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.deps import get_bedrock_client
from app.main import app
from app.models.scorecard_embedding import EMBEDDING_DIM
from tests.fakes import FakeBedrockClient


class _FixedVectorFakeBedrock(FakeBedrockClient):
    """Every `.embed()` call returns the same fixed vector, so the query is guaranteed to
    match whatever scorecard we seed with that same vector directly via the DB."""

    def __init__(self, vector: list[float]) -> None:
        super().__init__()
        self._vector = vector

    def embed(self, text: str, *, dimensions: int = 1024) -> list[float]:
        return self._vector


def test_suggest_similar_returns_ranked_matches(client: TestClient, seed_user_id: str) -> None:
    user = client.post(
        "/api/v1/users",
        json={"email": "suggest-similar@example.com", "name": "Suggest Similar"},
        headers={"X-User-Id": seed_user_id},
    ).json()
    scorecard = client.post(
        "/api/v1/scorecards",
        json={"name": "Support Ticket Quality", "owner_id": user["id"], "domain": "Support"},
        headers={"X-User-Id": user["id"]},
    ).json()
    version = client.post(
        f"/api/v1/scorecards/{scorecard['id']}/versions",
        json={"version_number": 1, "created_by": user["id"]},
        headers={"X-User-Id": user["id"]},
    ).json()

    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)

    # Seed the embedding directly via the DB (bypassing embeddings.py's own Bedrock call,
    # since this test is only exercising the HTTP + pgvector search path).
    import asyncio

    async def _seed():
        from app.config import get_settings
        from app.db import AsyncSessionLocal
        from app.models.scorecard_embedding import ScorecardEmbedding

        async with AsyncSessionLocal() as db:
            db.add(
                ScorecardEmbedding(
                    scorecard_version_id=version["id"],
                    embedding=vector,
                    embedding_model=get_settings().embedding_model_id,
                    source_text_hash="deadbeef",
                )
            )
            await db.commit()

    asyncio.run(_seed())

    app.dependency_overrides[get_bedrock_client] = lambda: _FixedVectorFakeBedrock(vector)
    try:
        r = client.post("/api/v1/scorecards/suggest-similar", json={"query": "support tickets"})
    finally:
        app.dependency_overrides.pop(get_bedrock_client, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Support Ticket Quality"
    assert body[0]["scorecard_id"] == scorecard["id"]
    assert body[0]["similarity"] > 0.99
