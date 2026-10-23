"""Embedding generation + upsert into `scorecard_embeddings`, for the "suggest similar
scorecard" feature (see `similarity.py` for the search side).

Source text is deliberately just `purpose + domain + KPI names` (per the plan: "a
scorecard's purpose+domain+KPI-name summary") — not guideline text — since guidelines are
long, per-level, and would dilute the embedding's signal for "is this the same *kind* of
scorecard", which is the question similarity search is answering.

Embeddings follow the AI provider: a version has at most ONE row per embedding model
(unique `(scorecard_version_id, embedding_model)`), so Titan rows (Bedrock) and
text-embedding-3-small rows (OpenRouter) live side by side and switching provider back and
forth reuses whatever is already there (`embedding_backfill.py` fills the gaps).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.bedrock_client import BedrockClientProtocol
from app.ai.providers import models_of
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion


def build_source_text(scorecard: Scorecard, kpi_names: list[str]) -> str:
    parts = [
        scorecard.purpose_statement or "",
        scorecard.domain or "",
        ", ".join(sorted(kpi_names)),
    ]
    return "\n".join(p for p in parts if p)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def scorecard_source_text(db: AsyncSession, scorecard_version_id: uuid.UUID) -> str:
    """The text a version is embedded from (see the module docstring). Raises ValueError for an unknown id."""
    version = await db.get(ScorecardVersion, scorecard_version_id)
    if version is None:
        raise ValueError(f"scorecard_version {scorecard_version_id} not found.")
    scorecard = await db.get(Scorecard, version.scorecard_id)
    if scorecard is None:
        raise ValueError(f"scorecard {version.scorecard_id} not found.")

    kpi_names = list(
        (
            await db.execute(
                select(KpiNode.name).where(KpiNode.scorecard_version_id == scorecard_version_id)
            )
        )
        .scalars()
        .all()
    )
    return build_source_text(scorecard, kpi_names)


async def upsert_scorecard_embedding(
    db: AsyncSession,
    scorecard_version_id: uuid.UUID,
    bedrock: BedrockClientProtocol,
    embedding_model_id: str | None = None,
) -> ScorecardEmbedding:
    """Embeds the version with `bedrock` and stores it as the version's row for that client's
    embedding model (`embedding_model_id` overrides it). An unchanged text is not re-embedded.
    Commits."""
    source_text = await scorecard_source_text(db, scorecard_version_id)
    text_hash = _hash(source_text)
    model_id = embedding_model_id or models_of(bedrock).embedding

    existing = (
        await db.execute(
            select(ScorecardEmbedding).where(
                ScorecardEmbedding.scorecard_version_id == scorecard_version_id,
                ScorecardEmbedding.embedding_model == model_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.source_text_hash == text_hash:
        return existing  # unchanged since last embed with this model — nothing to do

    # The clients are synchronous (boto3 / httpx): keep the event loop free.
    vector = await asyncio.to_thread(bedrock.embed, source_text)

    # Concurrent writers (a chat save racing a backfill) converge on one row per (version, model).
    stmt = pg_insert(ScorecardEmbedding).values(
        id=uuid.uuid4(),
        scorecard_version_id=scorecard_version_id,
        embedding=vector,
        embedding_model=model_id,
        source_text_hash=text_hash,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ScorecardEmbedding.scorecard_version_id, ScorecardEmbedding.embedding_model],
        set_={
            "embedding": stmt.excluded.embedding,
            "source_text_hash": stmt.excluded.source_text_hash,
            "updated_at": func.now(),
        },
    ).returning(ScorecardEmbedding.id)
    row_id = (await db.execute(stmt)).scalar_one()
    await db.commit()
    row = await db.get(ScorecardEmbedding, row_id, populate_existing=True)
    if row is None:  # the version (and so its rows, by cascade) was deleted a moment ago
        raise ValueError(f"scorecard_version {scorecard_version_id} was deleted while it was being embedded.")
    return row
