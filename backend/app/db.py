"""Database engines/sessions.

We use `psycopg` (v3) exclusively, via the `postgresql+psycopg` SQLAlchemy dialect, which
supports both sync and async connections from the same driver package. The async engine
backs the FastAPI request path; the sync engine backs one-off scripts (seed/generate_scenarios)
and the pytest suite, where a plain synchronous session keeps setup/teardown simple.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()
if not settings.database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Put it in the environment / .env, e.g. "
        "postgresql+psycopg://<user>:<password>@<host>:5432/<db> (see infra/.env.example)."
    )

# --- Async (FastAPI request path) ---
# Pool sizing is per PROCESS: with N API workers + M worker containers the database sees
# (N + M) x (pool_size + max_overflow) connections at most - keep that under Postgres max_connections
# (or put pgbouncer / RDS Proxy in front). All four knobs are env-configurable (DB_POOL_SIZE, ...).
_POOL = {
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    "pool_recycle": settings.db_pool_recycle_seconds,
    "pool_timeout": settings.db_pool_timeout_seconds,
}
engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True, **_POOL)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# --- Sync (scripts + tests) ---
sync_engine = create_engine(settings.database_url, pool_pre_ping=True, future=True, **_POOL)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False, future=True)


def get_sync_db() -> Generator[Session, None, None]:
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
