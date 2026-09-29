"""pgvector cosine-similarity search tests (app/ai/similarity.py), against **real**
Postgres/pgvector with synthetic vectors — no Bedrock call needed (per the task: this one
can run against real infrastructure without needing real Bedrock, since it's exercising
the SQL/pgvector layer, not embedding generation)."""

from __future__ import annotations

import uuid

from app.ai.similarity import find_similar_by_vector
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import EMBEDDING_DIM, ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User


async def _seed_scorecard_with_embedding(db, *, name: str, vector: list[float]) -> Scorecard:
    owner = User(email=f"sim-{uuid.uuid4().hex[:8]}@example.com", name="Similarity Test Owner")
    db.add(owner)
    await db.flush()
    scorecard = Scorecard(name=name, owner_id=owner.id, domain="Support")
    db.add(scorecard)
    await db.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db.add(version)
    await db.flush()
    db.add(
        ScorecardEmbedding(
            scorecard_version_id=version.id,
            embedding=vector,
            embedding_model="test-synthetic",
            source_text_hash="deadbeef",
        )
    )
    await db.commit()
    return scorecard


def _unit_vector(hot_index: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[hot_index] = 1.0
    return v


async def test_nearest_known_embedding_is_returned_above_threshold(async_db_session) -> None:
    db = async_db_session
    await _seed_scorecard_with_embedding(db, name="Support Ticket Quality", vector=_unit_vector(0))
    await _seed_scorecard_with_embedding(db, name="Completely Unrelated Scorecard", vector=_unit_vector(1))

    query = _unit_vector(0)
    query[1] = 0.05  # nudge slightly, still overwhelmingly closest to the first vector

    results = await find_similar_by_vector(db, query, top_n=5, threshold=0.5)

    assert len(results) == 1
    assert results[0].name == "Support Ticket Quality"
    assert results[0].similarity > 0.9


async def test_orthogonal_embedding_is_excluded_below_threshold(async_db_session) -> None:
    db = async_db_session
    await _seed_scorecard_with_embedding(db, name="Near Match", vector=_unit_vector(0))
    await _seed_scorecard_with_embedding(db, name="Orthogonal Non-Match", vector=_unit_vector(1))

    query = _unit_vector(0)

    # High threshold: only the (near-)identical vector qualifies.
    strict = await find_similar_by_vector(db, query, top_n=5, threshold=0.9)
    assert [r.name for r in strict] == ["Near Match"]

    # Zero threshold: both come back, but still ranked nearest-first.
    all_results = await find_similar_by_vector(db, query, top_n=5, threshold=0.0)
    assert [r.name for r in all_results][0] == "Near Match"
    assert len(all_results) == 2
    assert all_results[0].similarity > all_results[1].similarity
