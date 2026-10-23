"""Fills in `scorecard_embeddings` rows for the active provider's embedding model.

Embeddings follow the AI provider (Titan on Bedrock, text-embedding-3-small on OpenRouter), and vectors from
different models cannot be compared, so similarity search only looks at rows of the current model (see
`similarity.py`). After a provider switch, every version embedded only with the OTHER model would be invisible to
"suggest similar". This module embeds those versions with the current client:

- `schedule_backfill(client, reason=...)`: fire-and-forget. Called by the admin provider switch and after each
  similarity search. Skipped for test fakes (no embedding model of their own) and when the same model already ran
  in this process within the last 10 minutes.
- `backfill_missing_embeddings(client)`: one pass. A Postgres advisory lock per model (held on a dedicated
  AUTOCOMMIT connection) keeps several processes from embedding the same versions at once. Only versions that
  already have SOME embedding row but none for this model are embedded (a version that never had one was never
  meant to be suggested). At most `concurrency` embed calls run at a time, each upsert in its own session.
  Never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select, text
from sqlalchemy.orm import aliased

from app.ai.bedrock_client import BedrockClientProtocol
from app.ai.embeddings import upsert_scorecard_embedding
from app.ai.providers import embedding_model_of
from app.models.scorecard_embedding import ScorecardEmbedding

logger = logging.getLogger(__name__)

# pg_try_advisory_lock(namespace, hashtext(model)): the two-int4 form, so it cannot collide with the dispatcher's
# single-bigint claim lock.
_LOCK_NAMESPACE = 7_311_002
DEBOUNCE_SECONDS = 600.0

_tasks: set[asyncio.Task[int]] = set()  # asyncio keeps only weak references to tasks
_last_run: dict[str, float] = {}  # embedding model -> monotonic time its last backfill was scheduled


def schedule_backfill(client: BedrockClientProtocol, *, reason: str) -> bool:
    """Starts `backfill_missing_embeddings(client)` in the background; returns whether it was scheduled."""
    model = embedding_model_of(client)
    if model is None:
        return False  # a test fake: nothing to backfill for
    now = time.monotonic()
    last = _last_run.get(model)
    if last is not None and now - last < DEBOUNCE_SECONDS:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _last_run[model] = now
    task = loop.create_task(backfill_missing_embeddings(client), name=f"embedding-backfill:{model}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    logger.info("Scheduled embedding backfill for model %s (%s).", model, reason)
    return True


def reset() -> None:
    """Forgets the debounce state (tests)."""
    _last_run.clear()


async def wait_for_backfills() -> None:
    """Awaits every backfill task scheduled in this process (tests; graceful shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _versions_missing(model: str) -> list:
    """Versions with at least one embedding row, but none for `model`."""
    from app.db import AsyncSessionLocal  # lazily: importing this module must not create the engine

    other = aliased(ScorecardEmbedding)
    stmt = (
        select(ScorecardEmbedding.scorecard_version_id)
        .where(
            ~select(other.id)
            .where(
                other.scorecard_version_id == ScorecardEmbedding.scorecard_version_id,
                other.embedding_model == model,
            )
            .exists()
        )
        .distinct()
    )
    async with AsyncSessionLocal() as db:
        return list((await db.execute(stmt)).scalars().all())


async def backfill_missing_embeddings(client: BedrockClientProtocol, *, concurrency: int = 4) -> int:
    """One backfill pass for the client's embedding model; returns how many versions were embedded."""
    from app.db import engine  # lazily: importing this module must not create the engine

    model = embedding_model_of(client)
    if model is None:
        return 0
    key = {"ns": _LOCK_NAMESPACE, "model": model}
    try:
        # A session-level advisory lock on a dedicated AUTOCOMMIT connection: no transaction stays open while
        # the (possibly minutes-long) pass runs, and the lock is released on this same connection.
        async with engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:ns, hashtext(:model))"), key)).scalar()
            if not got:
                logger.info("Embedding backfill for %s is already running elsewhere; skipping.", model)
                return 0
            try:
                return await _backfill(client, model, max(1, concurrency))
            finally:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:ns, hashtext(:model))"), key)
                except Exception:  # noqa: BLE001 - drop the connection instead: closing it frees the lock
                    await raw.invalidate()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a background optimisation must never surface an error
        logger.warning("Embedding backfill for %s failed.", model, exc_info=True)
        return 0


async def _backfill(client: BedrockClientProtocol, model: str, concurrency: int) -> int:
    from app.db import AsyncSessionLocal

    started = time.monotonic()
    version_ids = await _versions_missing(model)
    if not version_ids:
        logger.info("Embedding backfill for %s: nothing to do.", model)
        return 0
    sem = asyncio.Semaphore(concurrency)
    failed = 0

    async def one(version_id) -> bool:
        nonlocal failed
        async with sem:
            try:
                async with AsyncSessionLocal() as db:
                    await upsert_scorecard_embedding(db, version_id, client, embedding_model_id=model)
                return True
            except Exception:  # noqa: BLE001 - one version must not stop the rest
                failed += 1
                logger.warning("Embedding backfill for %s: version %s failed.", model, version_id, exc_info=True)
                return False

    done = sum(await asyncio.gather(*(one(v) for v in version_ids)))
    logger.info(
        "Embedding backfill for %s: embedded %d of %d version(s), %d failed, in %.1fs.",
        model, done, len(version_ids), failed, time.monotonic() - started,
    )
    return done
