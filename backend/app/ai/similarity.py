"""pgvector cosine-similarity search over `scorecard_embeddings`, powering the "suggest
similar scorecard" feature.

`find_similar_by_vector` is the pure-DB half (query vector in, ranked rows out) and is
independently testable against real Postgres/pgvector with synthetic vectors, with no
Bedrock call needed — see `tests/test_similarity.py`. `find_similar_scorecards` is the
Bedrock-backed convenience wrapper that embeds free-text first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.bedrock_client import BedrockClientProtocol
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion

# Cosine SIMILARITY (1 - cosine distance) a candidate must meet to be surfaced as a
# suggestion. pgvector's `<=>` operator returns cosine *distance* (0 = identical,
# 2 = opposite), so similarity = 1 - distance. 0.75 is a deliberately conservative
# starting point for "close enough to suggest reuse/adapt" without drowning a genuinely
# new request in irrelevant suggestions; the plan flags this threshold as a UX gate
# ("similar-scorecard suggestions ... with a similarity threshold gate") to tune with
# real usage data — kept as a single named constant so that tuning is a one-line change.
SIMILARITY_THRESHOLD = 0.75


@dataclass
class SimilarScorecardResult:
    scorecard_id: uuid.UUID
    scorecard_version_id: uuid.UUID
    name: str
    domain: str | None
    similarity: float
    # Additive (Wave 3 integration): lets the frontend's reuse-suggestion card show a
    # one-line summary without a second round-trip to GET /scorecards/{id}.
    purpose_statement: str | None = None


async def find_similar_by_vector(
    db: AsyncSession,
    query_vector: list[float],
    *,
    top_n: int = 5,
    threshold: float = SIMILARITY_THRESHOLD,
    owner_id: uuid.UUID | None = None,
) -> list[SimilarScorecardResult]:
    distance = ScorecardEmbedding.embedding.cosine_distance(query_vector)
    stmt = (
        select(
            Scorecard.id,
            ScorecardEmbedding.scorecard_version_id,
            Scorecard.name,
            Scorecard.domain,
            Scorecard.purpose_statement,
            distance.label("distance"),
        )
        .join(ScorecardVersion, ScorecardVersion.id == ScorecardEmbedding.scorecard_version_id)
        .join(Scorecard, Scorecard.id == ScorecardVersion.scorecard_id)
        .where(Scorecard.deleted_at.is_(None))  # trashed charts are never suggested
        .order_by(distance)
        .limit(top_n)
    )
    if owner_id is not None:  # only the caller's own scorecards are ever suggested
        stmt = stmt.where(Scorecard.owner_id == owner_id)
    rows = (await db.execute(stmt)).all()
    results = [
        SimilarScorecardResult(
            scorecard_id=row.id,
            scorecard_version_id=row.scorecard_version_id,
            name=row.name,
            domain=row.domain,
            similarity=1.0 - row.distance,
            purpose_statement=row.purpose_statement,
        )
        for row in rows
    ]
    return [r for r in results if r.similarity >= threshold]


async def find_similar_scorecards(
    db: AsyncSession,
    bedrock: BedrockClientProtocol,
    query_text: str,
    *,
    top_n: int = 5,
    threshold: float = SIMILARITY_THRESHOLD,
    owner_id: uuid.UUID | None = None,
) -> list[SimilarScorecardResult]:
    query_vector = bedrock.embed(query_text)
    return await find_similar_by_vector(db, query_vector, top_n=top_n, threshold=threshold, owner_id=owner_id)
