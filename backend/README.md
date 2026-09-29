# Quality Scorecard System — Backend

Phase 0 + Cycle 1 (Data Foundation + core CRUD API) of the Quality Scorecard System, per
`plans/polished-swimming-sun.md`. FastAPI + SQLAlchemy 2.0 + Alembic + Postgres
(`pgvector` + `ltree`), driven entirely through `psycopg` (v3).

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2.0 (async for the API, sync for scripts/tests), Alembic
- Postgres 16/17 with the `vector` and `ltree` extensions (`pgvector/pgvector:pg17` locally)
- `psycopg[binary]` v3 as the only DB driver (matches `DATABASE_URL=postgresql+psycopg://...`)
- `ruff` for linting, `pytest` for tests (against a real Postgres DB — no mocks)

## Local (non-Docker) setup

1. **Postgres**: you need a real Postgres instance with `vector` and `ltree` available —
   easiest is the same image the project uses everywhere else:

   ```bash
   docker run -d --name qs_dev_pg -e POSTGRES_USER=qs_app -e POSTGRES_PASSWORD=qs_dev_password \
     -e POSTGRES_DB=quality_scorecard -p 5432:5432 pgvector/pgvector:pg17
   ```

2. **Python env** (from `backend/`), using [`uv`](https://docs.astral.sh/uv/) (or plain
   `pip` — either works, since `pyproject.toml` is a standard PEP 621 project):

   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv -e ".[dev]"
   # or, without uv:
   #   python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
   ```

3. **Environment variable** — everything reads `DATABASE_URL` (must be a `postgresql+psycopg://` URL):

   ```bash
   export DATABASE_URL="postgresql+psycopg://qs_app:qs_dev_password@localhost:5432/quality_scorecard"
   ```

   (Windows PowerShell: `$env:DATABASE_URL = "postgresql+psycopg://qs_app:qs_dev_password@localhost:5432/quality_scorecard"`)

4. **Run migrations**:

   ```bash
   python -m alembic upgrade head
   ```

5. **Seed the Cycle 1 scenario catalogue** (idempotent — see "Seeding" below):

   ```bash
   python -m app.scripts.seed
   ```

6. **Run the API**:

   ```bash
   python -m uvicorn app.main:app --reload
   ```

   `GET http://localhost:8000/health` should return `{"status": "ok"}`. Interactive docs
   at `http://localhost:8000/docs`.

7. **Run tests** against a real Postgres, but **not** the dev/demo database: the suite
   `TRUNCATE`s app tables between tests, which would wipe the seeded catalogue. By
   default `tests/conftest.py` automatically redirects to a sibling database named
   `quality_scorecard_test` **on the same Postgres instance** `DATABASE_URL` points at
   (same host/user/password, just a different database) — set `TEST_DATABASE_URL`
   explicitly instead if you want a different instance entirely (e.g. CI):

   ```bash
   # one-time setup: create the test DB (already done for you by
   # infra/db-init/02-create-test-db.sql on a fresh docker-compose volume)
   psql "$DATABASE_URL" -c "CREATE DATABASE quality_scorecard_test;"
   psql "postgresql://.../quality_scorecard_test" -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS ltree;"

   python -m pytest
   ```

   `pytest` runs Alembic migrations against `quality_scorecard_test` itself (not the dev
   DB), so it doesn't need to be kept in sync manually — just present with the same two
   extensions. Confirm dev data is untouched any time via
   `curl http://localhost:8000/api/v1/scorecards | jq length` before/after a test run.

8. **Lint**:

   ```bash
   python -m ruff check .
   ```

> **Windows note**: `psycopg`'s async mode is incompatible with Windows' default
> `ProactorEventLoop`. `app/main.py` (and `tests/conftest.py`) set
> `asyncio.WindowsSelectorEventLoopPolicy()` automatically on `sys.platform == "win32"`, so
> this is handled for you — no action needed, just documented here so it isn't a mystery.

## Docker

The backend is built/run via `infra/docker-compose.yml` (`../backend` build context,
service name `backend`). From `infra/`:

```bash
cp .env.example .env   # fill in real values as needed; POSTGRES_*/BACKEND_PORT defaults work as-is
docker compose up --build db backend
```

The container runs migrations are **not** run automatically on container start in Cycle 1
— run `alembic upgrade head` once against the compose `db` service (e.g.
`docker compose exec backend python -m alembic upgrade head`) after the stack is up, then
optionally `docker compose exec backend python -m app.scripts.seed`.

## Seeding

`python -m app.scripts.seed` implements the full Cycle 1 scenario catalogue from the plan
(normal/happy-path, business variants, lifecycle states, boundary cases, invalid/flawed
data, migration cases — see `app/scripts/generate_scenarios.py`). It is **idempotent**: a
marker user (`seed-marker@qualityscorecard.local`) signals "already seeded", and a second
run without flags just skips. Pass `--reset` to `TRUNCATE` every app table and reseed from
scratch:

```bash
python -m app.scripts.seed          # seeds once; no-ops if already seeded
python -m app.scripts.seed --reset  # wipes all app tables, then reseeds
```

"Invalid/flawed data" scenarios (e.g. sibling weights summing to 97%) are *expected* to be
rejected by a DB constraint/trigger — the script attempts the insert, catches the DB error,
and reports it as a **passing** validation (proof the constraint works), not a failure. The
printed report distinguishes `created` / `rejected_as_expected` / `flawed_but_inserted` /
`FAILED` per scenario.

## API

All routes are mounted under `/api/v1` (see `app/api/v1/router.py`): `users`,
`scorecards` (+ nested `versions`, and KPI node endpoints at `/api/v1/scorecard-versions/*`
and `/api/v1/kpi-nodes/*`, since KPI nodes are addressed by their own id once created), and
`evaluations` (+ nested `results`).

**Auth is a dev-only stub** (`app/deps.py::get_current_user`): it trusts an `X-User-Id`
header (a raw user UUID) or a `Authorization: Bearer <user-id>` token, with no signature
verification. This is explicitly **not** production auth — real OIDC is deferred per the
plan. It is applied uniformly to every mutating route (every `POST`/`PATCH`/`DELETE`)
across every router — `users`, `scorecards` (+ versions), `kpi-nodes` (+ guidelines,
including the bulk endpoints), `evaluations` (+ results, + `/run`), and `chat` (both
starting and continuing a session) — so every write has an identity attached. The one
deliberate exception is `POST /api/v1/scorecards/suggest-similar`, which is a read-only
search/query endpoint (POST only because it takes a query body), not a create/update/delete.
Note the bootstrap implication: since `POST /api/v1/users` itself now requires an existing
authenticated user, the very first user in a fresh database must be created directly (the
seed script does this — see "Seeding" below — it writes `User` rows directly via
SQLAlchemy, never through the HTTP API) rather than through this endpoint.

**Weight-sum trigger and the API**: because the weight-sum-to-100 rule is a *deferred*
constraint trigger that only fires at transaction `COMMIT`, and each HTTP request is its
own transaction, creating a full sibling KPI group must go through the bulk endpoints
(`POST /api/v1/scorecard-versions/{version_id}/kpi-nodes/bulk`,
`PATCH /api/v1/kpi-nodes/weights`) rather than one-node-at-a-time — see the docstring at
the top of `app/api/v1/kpi_nodes.py` for the full explanation.
