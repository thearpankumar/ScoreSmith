"""Embedding generation + upsert into `scorecard_embeddings`, for the "suggest
similar scorecard" feature (see `similarity.py` for the search side).

Source text is deliberately just `purpose + domain + KPI names` (per the plan: "a
scorecard's purpose+domain+KPI-name summary") — not guideline text — since guidelines are
long, per-level, and would dilute the embedding's signal for "is this the same *kind* of
scorecard", which is the question similarity search is answering.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.bedrock_client import BedrockClientProtocol
from app.config import get_settings
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


async def upsert_scorecard_embedding(
    db: AsyncSession,
    scorecard_version_id: uuid.UUID,
    bedrock: BedrockClientProtocol,
    embedding_model_id: str | None = None,
) -> ScorecardEmbedding:
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
    source_text = build_source_text(scorecard, kpi_names)
    text_hash = _hash(source_text)
    model_id = embedding_model_id or get_settings().embedding_model_id

    existing = (
        (
            await db.execute(
                select(ScorecardEmbedding).where(
                    ScorecardEmbedding.scorecard_version_id == scorecard_version_id
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if (
        existing is not None
        and existing.source_text_hash == text_hash
        and existing.embedding_model == model_id
    ):
        return existing  # unchanged since last embed — nothing to do

    # The client is synchronous (httpx / boto3): keep the event loop free.
    vector = await asyncio.to_thread(bedrock.embed, source_text)

    if existing is not None:
        existing.embedding = vector
        existing.embedding_model = model_id
        existing.source_text_hash = text_hash
        row = existing
    else:
        row = ScorecardEmbedding(
            scorecard_version_id=scorecard_version_id,
            embedding=vector,
            embedding_model=model_id,
            source_text_hash=text_hash,
        )
        db.add(row)

    await db.commit()
    await db.refresh(row)
    return row
