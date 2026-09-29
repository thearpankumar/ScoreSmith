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

# --- Async (FastAPI request path) ---
engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# --- Sync (scripts + tests) ---
sync_engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False, future=True)


def get_sync_db() -> Generator[Session, None, None]:
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
