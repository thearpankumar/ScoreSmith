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
   export PGPW="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"   # keep it; also used in step 3
   docker run -d --name qs_dev_pg -e POSTGRES_USER=qs_app -e POSTGRES_PASSWORD="$PGPW" \
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
   export DATABASE_URL="postgresql+psycopg://qs_app:${PGPW}@localhost:5432/quality_scorecard"
   ```

   (Windows PowerShell: `$env:DATABASE_URL = "postgresql+psycopg://qs_app:<your password>@localhost:5432/quality_scorecard"`)

   There is no default database password and no default `JWT_SECRET` in the code. Outside production an unset
   `JWT_SECRET` becomes an ephemeral random key (with a warning); set `JWT_SECRET` for anything long-lived.

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

All routes are mounted under `/api/v1` (see `app/api/v1/router.py`): `auth` (signup, login, refresh, logout,
logout-all, forgot / reset password, verify e-mail, OAuth), `me`, `scorecards` (+ nested `versions`, and KPI node
endpoints at `/api/v1/scorecard-versions/*` and `/api/v1/kpi-nodes/*`, since KPI nodes are addressed by their own id
once created), `evaluations` (+ nested `results`, AI jobs / uploads / progress / export) and `chat`.

**Authentication** (`app/auth/`, `app/deps.py::get_current_user`): a 15-minute HS256 access JWT (`sub`, `exp`,
`jti`) plus an opaque, rotating refresh token stored hashed (replaying an already-rotated token revokes the whole
family). The browser gets them as httpOnly, SameSite=Lax cookies (`Secure` in production) plus a readable CSRF
cookie: cookie-authenticated `POST`/`PATCH`/`PUT`/`DELETE` must send the same value in `X-CSRF-Token` and a friendly
`Origin`. Scripts can use `Authorization: Bearer <access_token>` from `POST /auth/login` (no CSRF needed). Passwords
are Argon2id (OWASP profile: 19 MiB, 2 iterations, 1 lane), minimum 12 characters. Failed logins lock the account
with doubling backoff; auth routes are rate limited per IP (and logins per e-mail), expensive routes (chat, AI jobs,
uploads, export) per user - Redis-backed when `REDIS_URL` is set, in-process otherwise. The old `X-User-Id` header
and `Bearer <user-uuid>` are gone. Run with `ENV=production` the API and worker refuse to start with a
missing/weak `JWT_SECRET`/DB password, `localhost`/`*` CORS or frontend URL, or `EMAIL_BACKEND=log`; `/docs` is off,
cookies are `Secure` and HSTS is sent. (`ENVIRONMENT=production` is accepted as an alias of `ENV=production`.)

**Ownership** (`app/authz.py`): every resource belongs to one user and every endpoint that takes an id loads it
through an owner-scoped helper that answers **404** (never 403) for someone else's data. Scorecards, versions, KPI
nodes and guidelines follow `scorecards.owner_id`; evaluations follow `evaluations.owner_id` *or* the owner of their
scorecard (a scorecard owner sees every evaluation of it, an evaluator only their own); batches `created_by`; chat
sessions `user_id`. Owner / evaluator ids are always taken from the token, never from the request body. Upload
keys (`uploads/{user}/...`, `batches/{user}/...`) are bound to the user they were issued to. There is no public user
directory: `GET`/`PATCH /api/v1/me` only.

**First admin and user management** (`app/auth/bootstrap.py`, `POST /api/v1/auth/register-user`). Roles: `admin`
and `member` (any other legacy value counts as an ordinary user). The role is never accepted from signup (a `role`
field is a 422) or `PATCH /me` (ignored); only an admin can grant `admin`.

- *Development* (`ENV` not `production`): set `ADMIN_EMAIL` + `ADMIN_PASSWORD` in `infra/.env`. On startup the api and
  every worker replica create that admin (email-verified, Argon2id) if no user has the address - serialised by a
  Postgres advisory lock, so replicas never create two, and an existing user is never touched (no password reset).
  The password must meet the admin policy (14+ characters with 3 character classes, or a 20+ character passphrase)
  and is never logged.
- *Production* (`ENV=production`): `ADMIN_EMAIL`/`ADMIN_PASSWORD` are **ignored** (a warning is logged). Set a
  one-time `BOOTSTRAP_TOKEN` (32+ random characters), then create the first admin once:
  `curl -X POST https://HOST/api/v1/auth/register-user -H 'Content-Type: application/json' -H "X-Bootstrap-Token: $BOOTSTRAP_TOKEN" -d '{"email":"admin@corp.com","name":"Admin","password":"<strong>"}'`
  (or open `/setup` in the web app). It only works while **zero** users exist, compares the token in constant
  time, is rate limited (`RATE_LIMIT_REGISTER_USER`, 5/min per IP), audit logged (`bootstrap_token_rejected`,
  `bootstrap_admin_created`) and answers 404 for good once any user exists. Remove `BOOTSTRAP_TOKEN` afterwards.
- *Afterwards*: a signed-in admin creates users with the same endpoint (`{"email","name","password","role"}`, role
  `member` by default). Public signup (and OAuth account creation) is controlled by `SIGNUP_ENABLED` - default on
  in development, **off in production**; the web app hides "Sign up" when it is off.
- `python -m app.scripts.set_password <email> [--create] [--role admin]` remains as an operator CLI (recover an
  admin, claim an account that has no password, e.g. rows created by the seed script).

**Existing data**: migration `0012` does not rewrite ownership. Rows created before login existed (such as the
seed script's demo owner) belong to a user without a password: claim it with `set_password <that email>` and sign in
as it; anyone else signs up (or is registered by an admin) and starts with an empty workspace. In local dev
(`EMAIL_BACKEND=log`) reset / verification links are written to the API log.

**Weight-sum trigger and the API**: because the weight-sum-to-100 rule is a *deferred*
constraint trigger that only fires at transaction `COMMIT`, and each HTTP request is its
own transaction, creating a full sibling KPI group must go through the bulk endpoints
(`POST /api/v1/scorecard-versions/{version_id}/kpi-nodes/bulk`,
`PATCH /api/v1/kpi-nodes/weights`) rather than one-node-at-a-time — see the docstring at
the top of `app/api/v1/kpi_nodes.py` for the full explanation.

