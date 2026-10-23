"""Embeddings follow the AI provider: one `scorecard_embeddings` row per (version, embedding model), similarity
search compares only rows of the searching client's model, and `embedding_backfill` fills in the current model's
rows for versions that only have the other provider's. Real Postgres/pgvector, fake clients."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from app.ai import embedding_backfill as backfill
from app.ai.embeddings import build_source_text, upsert_scorecard_embedding
from app.ai.providers import embedding_model_of
from app.ai.similarity import find_similar_by_vector, find_similar_scorecards
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.models.scorecard import Scorecard
from app.models.scorecard_embedding import EMBEDDING_DIM, ScorecardEmbedding
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from tests.fakes import FakeBedrockClient, FakeOpenRouterClient

TITAN = "amazon.titan-embed-text-v2:0"


@pytest.fixture(autouse=True)
def _fresh_debounce():
    backfill.reset()
    yield
    backfill.reset()


def _unit(hot: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[hot] = 1.0
    return v


async def _owner(db) -> User:
    owner = User(email=f"emb-{uuid.uuid4().hex[:8]}@example.com", name="Embedding Owner")
    db.add(owner)
    await db.flush()
    return owner


async def _version(db, owner: User, *, name: str = "Support Quality", purpose: str = "Rate tickets."):
    scorecard = Scorecard(name=name, owner_id=owner.id, domain="Support", purpose_statement=purpose)
    db.add(scorecard)
    await db.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db.add(version)
    await db.flush()
    scorecard.current_version_id = version.id
    await db.commit()
    return version


async def _add_row(db, version: ScorecardVersion, model: str, vector: list[float] | None = None) -> None:
    db.add(
        ScorecardEmbedding(
            scorecard_version_id=version.id, embedding=vector or _unit(0), embedding_model=model,
            source_text_hash="seed",
        )
    )
    await db.commit()


async def _rows(version_id) -> dict[str, ScorecardEmbedding]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(ScorecardEmbedding).where(ScorecardEmbedding.scorecard_version_id == version_id))
        ).scalars().all()
    return {r.embedding_model: r for r in rows}


# --- similarity filter ----------------------------------------------------------------------------------------------


async def test_similarity_filters_on_the_embedding_model_only_when_given(async_db_session) -> None:
    db = async_db_session
    owner = await _owner(db)
    titan = await _version(db, owner, name="Titan Card")
    other = await _version(db, owner, name="OpenRouter Card")
    await _add_row(db, titan, "model-a")
    await _add_row(db, other, "model-b")

    unfiltered = await find_similar_by_vector(db, _unit(0), threshold=0.5)
    assert {r.name for r in unfiltered} == {"Titan Card", "OpenRouter Card"}
    only_a = await find_similar_by_vector(db, _unit(0), threshold=0.5, embedding_model="model-a")
    assert [r.name for r in only_a] == ["Titan Card"]
    assert await find_similar_by_vector(db, _unit(0), threshold=0.5, embedding_model="nobody") == []


async def test_find_similar_scorecards_compares_only_the_clients_model(async_db_session) -> None:
    db = async_db_session
    owner = await _owner(db)
    fake = FakeOpenRouterClient()
    query = "rate support tickets"
    vector = fake.embed(query)
    same = await _version(db, owner, name="Same Model")
    other = await _version(db, owner, name="Other Model")
    await _add_row(db, same, embedding_model_of(fake), vector)
    await _add_row(db, other, TITAN, vector)  # identical numbers, but from another model's space

    found = await find_similar_scorecards(db, fake, query, owner_id=owner.id)
    assert [r.name for r in found] == ["Same Model"]
    # A fake with no model of its own (FakeBedrockClient) is not filtered.
    assert {r.name for r in await find_similar_scorecards(db, FakeBedrockClient(), query, threshold=0.0)} == {
        "Same Model", "Other Model",
    }


# --- per-model upserts ----------------------------------------------------------------------------------------------


async def test_embeddings_of_two_models_coexist_on_one_version(async_db_session) -> None:
    db = async_db_session
    version = await _version(db, await _owner(db))
    bedrock, openrouter = FakeBedrockClient(), FakeOpenRouterClient()

    titan_row = await upsert_scorecard_embedding(db, version.id, bedrock)
    or_row = await upsert_scorecard_embedding(db, version.id, openrouter)
    assert titan_row.embedding_model == get_settings().bedrock_embedding_model_id
    assert or_row.embedding_model == embedding_model_of(openrouter)
    rows = await _rows(version.id)
    assert set(rows) == {titan_row.embedding_model, or_row.embedding_model}

    # Unchanged text: the same row comes back, nothing is re-embedded or duplicated.
    again = await upsert_scorecard_embedding(db, version.id, openrouter)
    assert again.id == or_row.id and len(await _rows(version.id)) == 2


async def test_a_changed_text_updates_only_that_models_row_in_place(async_db_session) -> None:
    db = async_db_session
    version = await _version(db, await _owner(db))
    bedrock, openrouter = FakeBedrockClient(), FakeOpenRouterClient()
    titan_row = await upsert_scorecard_embedding(db, version.id, bedrock)
    titan_model, titan_hash = titan_row.embedding_model, titan_row.source_text_hash
    or_row = await upsert_scorecard_embedding(db, version.id, openrouter)
    or_id, or_hash = or_row.id, or_row.source_text_hash

    scorecard = await db.get(Scorecard, (await db.get(ScorecardVersion, version.id)).scorecard_id)
    scorecard.purpose_statement = "Something else entirely."
    await db.commit()
    updated = await upsert_scorecard_embedding(db, version.id, openrouter)

    assert updated.id == or_id and updated.source_text_hash != or_hash
    rows = await _rows(version.id)
    assert len(rows) == 2
    assert rows[titan_model].source_text_hash == titan_hash  # the other model's row is untouched


async def test_an_explicit_embedding_model_id_overrides_the_clients(async_db_session) -> None:
    db = async_db_session
    version = await _version(db, await _owner(db))
    row = await upsert_scorecard_embedding(db, version.id, FakeBedrockClient(), embedding_model_id="custom-model")
    assert row.embedding_model == "custom-model"


# --- backfill -------------------------------------------------------------------------------------------------------


async def test_schedule_backfill_skips_fakes_without_an_embedding_model() -> None:
    assert backfill.schedule_backfill(FakeBedrockClient(), reason="test") is False
    assert await backfill.backfill_missing_embeddings(FakeBedrockClient()) == 0


async def test_schedule_backfill_is_debounced_per_model(async_db_session) -> None:
    fake = FakeOpenRouterClient()
    assert backfill.schedule_backfill(fake, reason="first") is True
    assert backfill.schedule_backfill(fake, reason="second") is False  # same model within 10 minutes
    await backfill.wait_for_backfills()
    backfill.reset()
    assert backfill.schedule_backfill(fake, reason="after reset") is True
    await backfill.wait_for_backfills()


async def test_backfill_embeds_only_versions_missing_the_current_model(async_db_session) -> None:
    db = async_db_session
    owner = await _owner(db)
    fake = FakeOpenRouterClient()
    model = embedding_model_of(fake)
    titan_only = [await _version(db, owner, name=f"Titan {i}", purpose=f"Purpose {i}") for i in range(3)]
    for v in titan_only:
        await _add_row(db, v, TITAN)
    both = await _version(db, owner, name="Both")
    await _add_row(db, both, TITAN)
    await _add_row(db, both, model)
    never = await _version(db, owner, name="Never embedded")

    assert await backfill.backfill_missing_embeddings(fake, concurrency=2) == 3

    for v in titan_only:
        rows = await _rows(v.id)
        assert set(rows) == {TITAN, model}
        scorecard = await db.get(Scorecard, v.scorecard_id)
        expected = fake.embed(build_source_text(scorecard, []))
        assert [round(x, 6) for x in rows[model].embedding] == [round(x, 6) for x in expected]
        assert rows[TITAN].source_text_hash == "seed"  # the other model's row is left alone
    assert (await _rows(both.id))[model].source_text_hash == "seed"  # already had one: not re-embedded
    assert await _rows(never.id) == {}  # never meant to be suggested: not embedded

    assert await backfill.backfill_missing_embeddings(fake) == 0  # nothing left


async def test_backfill_survives_a_failing_version(async_db_session, monkeypatch) -> None:
    db = async_db_session
    owner = await _owner(db)
    good, bad = await _version(db, owner, name="Good"), await _version(db, owner, name="Bad", purpose="explode")
    await _add_row(db, good, TITAN)
    await _add_row(db, bad, TITAN)

    class Flaky(FakeOpenRouterClient):
        def embed(self, text_: str, *, dimensions: int = 1024) -> list[float]:
            if "explode" in text_:
                raise RuntimeError("embedding endpoint down")
            return super().embed(text_, dimensions=dimensions)

    assert await backfill.backfill_missing_embeddings(Flaky()) == 1
    assert embedding_model_of(Flaky()) in await _rows(good.id)
    assert embedding_model_of(Flaky()) not in await _rows(bad.id)


async def test_backfill_skips_while_another_process_holds_the_models_lock(async_db_session) -> None:
    db = async_db_session
    version = await _version(db, await _owner(db))
    await _add_row(db, version, TITAN)
    fake = FakeOpenRouterClient()
    key = {"ns": backfill._LOCK_NAMESPACE, "model": embedding_model_of(fake)}
    async with engine.connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        assert (await conn.execute(text("SELECT pg_try_advisory_lock(:ns, hashtext(:model))"), key)).scalar()
        try:
            assert await backfill.backfill_missing_embeddings(fake) == 0
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:ns, hashtext(:model))"), key)
    assert await backfill.backfill_missing_embeddings(fake) == 1  # the lock was released


async def test_concurrent_backfills_never_duplicate_rows(async_db_session) -> None:
    db = async_db_session
    owner = await _owner(db)
    versions = [await _version(db, owner, name=f"Card {i}", purpose=f"P{i}") for i in range(4)]
    for v in versions:
        await _add_row(db, v, TITAN)
    fake = FakeOpenRouterClient()
    counts = await asyncio.gather(*(backfill.backfill_missing_embeddings(fake) for _ in range(3)))
    assert sum(counts) == 4
    for v in versions:
        assert set(await _rows(v.id)) == {TITAN, embedding_model_of(fake)}
