"""Pytest fixtures.

Testing philosophy for this project: use a real Postgres database, never mocks (per the
task's own instruction). `DATABASE_URL` must point at a Postgres instance with the
`vector` and `ltree` extensions available (e.g. the same `pgvector/pgvector:pg17` image
used by docker-compose) — a disposable test DB is fine, but it must be real Postgres.

At session start we run Alembic migrations up to `head` programmatically, so the schema
(including the weight-sum and cross-version-reference triggers) is guaranteed present.
Each test then gets a truncated, empty set of app tables so tests are isolated from each
other without relying on transaction rollback — which would hide deferred-constraint
trigger behaviour that is exactly what several tests need to exercise (a DEFERRED
CONSTRAINT TRIGGER only fires at a real COMMIT).

**Test database, separate from dev/demo** (see infra/db-init/02-create-test-db.sql and
infra/.env.example's `TEST_DATABASE_URL`): the `_clean_tables` fixture below TRUNCATEs
every app table before each test, which would silently wipe out the seeded dev/demo
catalogue (28 scorecards etc.) if pytest ever ran against the same database a developer
is manually poking at. To make that impossible by default, this module rewrites the
`DATABASE_URL` environment variable — *before any `app.*` module is imported* (both
`app/db.py` and `app/config.py` cache their engine/settings at import time) — to point at
a dedicated `quality_scorecard_test` database on the same Postgres instance:
- `TEST_DATABASE_URL`, if set, is used verbatim (e.g. for CI or a fully separate host).
- Otherwise, it's derived from `DATABASE_URL` by swapping the database name for
  `quality_scorecard_test`, on the same host/instance `DATABASE_URL` already points at.
`quality_scorecard_test` must already exist (created by the `db-init` script on a fresh
volume, or manually via `CREATE DATABASE quality_scorecard_test OWNER qs_app;` against an
already-initialized instance) — Postgres can't `CREATE DATABASE ... IF NOT EXISTS` inside
a transaction/init script easily, so this module does not attempt to create it itself;
Alembic will fail clearly (database does not exist) if it's missing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _default_test_database_url() -> str | None:
    """Swap DATABASE_URL's database name for `quality_scorecard_test`, on the same
    host/instance — the "simplest approach" default per the task: one extra database on
    the same Postgres service, not a second service."""
    base = os.environ.get("DATABASE_URL")
    if not base or "/" not in base:
        return None
    prefix, _, _dbname = base.rpartition("/")
    return f"{prefix}/quality_scorecard_test"


def _apply_test_database_url() -> None:
    """Must run at module import time, before any `app.*` module is imported anywhere in
    the test session (pytest imports conftest.py first, ahead of every test module and
    fixture) — see the module docstring for why."""
    override = os.environ.get("TEST_DATABASE_URL") or _default_test_database_url()
    if override:
        os.environ["DATABASE_URL"] = override


_apply_test_database_url()

# Chat turns default to BACKGROUND tasks (202 + polling) in production; the pre-existing API tests
# assert on the inline 200/201 responses, so the suite runs inline by default and the background
# tests (tests/test_chat_background_turns.py) opt in with `?wait=false`.
os.environ.setdefault("CHAT_TURNS_INLINE", "true")
# No background AI-evaluation dispatcher loop in tests: they drive `Dispatcher.run_until_idle()` themselves.
os.environ.setdefault("AI_EVAL_INLINE", "true")

APP_TABLES = [
    "audit_log",
    "chat_messages",
    "chat_turn_events",
    "chat_sessions",
    "evaluation_kpi_results",
    "evaluation_events",
    "evaluation_sources",
    "evaluations",
    "evaluation_batches",
    "scorecard_embeddings",
    "kpi_guidelines",
    "kpi_nodes",
    "scorecard_versions",
    "scorecards",
    "users",
]


@pytest.fixture(scope="session", autouse=True)
def _require_database_url() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.exit(
            "DATABASE_URL is not set. Point it at a real Postgres instance "
            "(postgresql+psycopg://...) with the vector and ltree extensions — "
            "this test suite does not use mocks."
        )


@pytest.fixture(scope="session")
def _migrated_db(_require_database_url: None):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def _clean_tables(_migrated_db: None) -> None:
    # Imported lazily, after _migrated_db, so the engine is created only once the schema
    # is known to exist (and after any Windows event-loop policy fix above is in place).
    from app.db import sync_engine

    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE;"))


@pytest.fixture()
def db_session(_clean_tables: None) -> Session:
    from app.db import SyncSessionLocal

    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(_clean_tables: None):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_ai_graph_manager():
    """`app.ai.scorecard_builder.GraphManager` is a module-level singleton holding one
    long-lived `AsyncPostgresSaver` connection — correct for a real ASGI app (one event
    loop for the process lifetime), but pytest-asyncio gives each test function its own
    event loop, and an asyncio.Lock/connection created under one loop cannot be reused
    under another. Resetting the singleton around every test forces a fresh connection
    bound to that test's own loop, so this is a test-harness accommodation only — nothing
    about production behavior changes."""
    import app.ai.scorecard_builder as sb

    sb._graph_manager = None
    yield
    sb._graph_manager = None


@pytest.fixture()
def seed_user_id(db_session: Session) -> str:
    """A real, already-persisted user id, inserted directly via the DB (bypassing the
    API) — for tests that need to authenticate the very first `POST /api/v1/users` call
    in an otherwise-empty test DB. `get_current_user` (the dev auth stub — see
    app/deps.py) is applied to every mutating route including user creation itself, so
    there is no way to create the *first* user through the API alone; this mirrors how
    `test_suggest_similar_api.py` already seeds a row directly via the DB when the HTTP
    layer isn't what's under test."""
    from app.models.user import User

    user = User(email="bootstrap@qualityscorecard.local", name="Bootstrap User")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return str(user.id)


@pytest.fixture()
async def async_db_session(_clean_tables: None):
    """Async counterpart to `db_session`, for AI-layer code that takes an AsyncSession
    (app/ai/draft_materialize.py, judge.py, embeddings.py, similarity.py)."""
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session
