# Score Smith: backend

FastAPI + async SQLAlchemy 2.0 + Alembic on Postgres 17 (`pgvector` + `ltree`), driven entirely through `psycopg` v3. It serves the REST API
(`ROLE=api`) and runs the long jobs (`ROLE=worker`): the AI-evaluation dispatcher and background chat turns. Overview of the whole
system: [../README.md](../README.md) and [../docs/architecture.md](../docs/architecture.md). Route table: [../docs/api-reference.md](../docs/api-reference.md).
Every setting: [../docs/configuration.md](../docs/configuration.md).

## Layout

| Path | What |
|---|---|
| `app/main.py` | App factory, lifespan, security headers, CORS, `/health`, `/ready` |
| `app/worker.py` | `python -m app.worker`: dispatcher + chat-turn runner + `/health /ready /metrics` on `WORKER_HEALTH_PORT` |
| `app/api/v1/` | Routers: `auth`, `oauth`, `admin_users`, `scorecards`, `kpi_nodes`, `sharing`, `trash`, `notifications`, `evaluations`, `evaluations_ai`, `evaluations_page`, `evaluations_export`, `chat`, `chat_shares` |
| `app/auth/` | Argon2id + JWT + cookies + CSRF (`security.py`), sessions and lockout (`service.py`), first-admin bootstrap (`bootstrap.py`), handle resolution (`handles.py`) |
| `app/authz.py`, `app/occ.py`, `app/slots.py`, `app/trash.py`, `app/notifications.py`, `app/activity.py`, `app/user_admin.py` | Access rules, `If-Match` checks, per-user slots, trash, notifications, editing log, admin user operations |
| `app/pipeline/` | Evaluation dispatcher (leases), scoring graph, Step Functions client, adaptive limiter, spreadsheet parser |
| `app/ai/` | Bedrock client, chat builder graph (LangGraph), judge, Jev client, web search, embeddings |
| `app/limits/redis_semaphore.py`, `app/ratelimit.py`, `app/metrics.py`, `app/idempotency.py` | Cluster-wide LLM limits (fail open), rate limits, backlog metric, `Idempotency-Key` |
| `app/models/`, `app/schemas/` | ORM models and Pydantic schemas |
| `app/scripts/` | CLIs (below) |
| `alembic/versions/` | Migrations `0001` to `0016` |
| `tests/` | pytest suite (~900 tests) against a real Postgres |

## Run it

Normally through docker compose ([../infra/README.md](../infra/README.md)): `migrate` applies migrations, then `backend` (gunicorn + uvicorn) and `worker` start.

Without Docker (needs a Postgres with `vector` and `ltree`, for example `pgvector/pgvector:pg17`):

```bash
cd backend
uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev]"     # or: python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export DATABASE_URL="postgresql+psycopg://qs_app:<your password>@127.0.0.1:5432/quality_scorecard"   # PowerShell: $env:DATABASE_URL = "..."
export JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='<a strong password>'          # dev only: creates the first admin on startup
python -m app.scripts.migrate                       # Alembic to head + LangGraph checkpoint tables
python -m uvicorn app.main:app --reload             # ROLE=all (default): API and jobs in one process
# or the split, as in production:
ROLE=api python -m uvicorn app.main:app
ROLE=worker python -m app.worker
```

`GET http://localhost:8000/health` returns `{"status":"ok"}`; interactive docs at `/docs` (development only). On Windows with Postgres in Docker, use `127.0.0.1` rather than `localhost` in
`DATABASE_URL` (`localhost` made the API tests very slow). `psycopg` async needs the selector event loop; `app/main.py`, `app/worker.py` and
`tests/conftest.py` set it automatically on Windows. Plain `uvicorn` creates its loop before that runs, so use `--reload`/the module entrypoint as above.

## Process roles

| `ROLE` | Serves HTTP | Runs dispatcher + chat turns | Used for |
|---|---|---|---|
| `api` | yes | no (jobs are only enqueued in Postgres) | the `backend` compose service |
| `worker` | no (only the health server) | yes | the `worker` compose service; run N replicas |
| `all` | yes | yes | local dev, the test suite |

Both roles run `Settings.validate_production_settings()` at start, so a worker with weak secrets cannot boot in production either.
Lease protocol, cancel flags, retries and the autoscaling metric are described in [../docs/architecture.md](../docs/architecture.md#2-evaluation-pipeline-and-lease-lifecycle).
Worker endpoints: `GET /health` (liveness, running counts, `draining`), `GET /ready` (Postgres, not draining), `GET /metrics` (backlog per worker).

## Command-line tools

Run inside the backend container (`docker compose exec backend python -m app.scripts.<name> ...`) or in your venv.

| Command | What it does | Flags |
|---|---|---|
| `python -m app.scripts.migrate` | Release job: Alembic `upgrade head`, then LangGraph checkpoint tables. Non-zero exit blocks the rollout | none |
| `python -m app.scripts.set_password <email>` | Set (or `--create`) an account password, mark the e-mail verified, revoke its sessions. Prompts twice (hidden); never prints it | `--password-stdin`, `--create`, `--name NAME`, `--role {admin,user}`, `--allow-weak` (dev only, refused in production) |
| `python -m app.scripts.share_everything --to <handle>` | Add one user as editor of every chart and read-only recipient of every chat (idempotent, audit logged, one notification per share, ownership unchanged) | `--to` (required, username / e-mail / unique local-part), `--dry-run` |
| `python -m app.scripts.purge_users --pattern 'e2e-%@example.com'` | Hard-delete leftover test accounts that own nothing. Dry run unless `--yes`. Never touches admins, the `system` user, `*@qualityscorecard.local` seed accounts | `--pattern` (repeatable, needs `@`, literal domain and a 3+ character literal prefix), `--dry-run`, `--yes` |
| `python -m app.scripts.seed` | Idempotent Cycle 1 scenario catalogue (demo data, owned by `designer@qualityscorecard.local`, which has no password; claim it with `set_password`) | `--reset` **truncates every app table** and reseeds: never on data you want to keep |

## Authentication

- **Tokens.** A 15-minute HS256 access JWT (claims `sub`, `exp`, `jti`, `rol`) and an opaque rotating refresh token stored hashed. Browser cookies:
  `qs_access` and `qs_refresh` (httpOnly, `Secure` in production, SameSite `lax`) and `qs_csrf` (readable). Unsafe cookie-authenticated requests must send
  `X-CSRF-Token` equal to the cookie plus an allowed `Origin`. Scripts may use `Authorization: Bearer <access_token>`. Replaying an already-rotated refresh
  token (outside the 10 s two-tab grace) revokes the whole token family. There is no `X-User-Id` header in the server; the test client converts it into a signed bearer token.
- **Passwords.** Argon2id (OWASP profile), 12 to 128 characters (admins 14+), common-password and e-mail-as-password checks. Lockout: 5 failures lock for 5 minutes, doubling up to 60.
  Per-IP and per-address rate limits (`RATE_LIMIT_LOGIN`, default 10/minute, then `429`).
- **Identifiers.** Sign in with the e-mail, the username, or the part of the e-mail before `@` when exactly one account has it (`app/auth/handles.py`). `ADMIN_USERNAME`
  (default `admin`) is an extra, **development-only** alias for the `ADMIN_EMAIL` account.
- **Flows.** Forgot / reset password and e-mail verification (links go to the server log with `EMAIL_BACKEND=log`, to Amazon SES with `ses`). OAuth (Google, GitHub, Microsoft) is
  wired but each provider is disabled until both its client id and secret are set; the provider round-trips have not been exercised against live providers.
- **First admin.** Development: `ADMIN_EMAIL` + `ADMIN_PASSWORD` in `infra/.env` create it on startup (advisory-locked, never touches an existing user). Production: those
  variables are ignored with a warning; set a one-time `BOOTSTRAP_TOKEN` (32+ chars) and call `POST /api/v1/auth/register-user` with header `X-Bootstrap-Token` (or open `/setup`).
  It works only while **zero** users exist, compares in constant time, is rate limited and audit logged, and answers 404 for good afterwards. Remove the token.
- **Signup.** `SIGNUP_ENABLED` defaults to on in development and **off in production**; then an admin creates accounts (`/admin/users` or `register-user`).
- **Production startup validation.** `ENV=production` refuses to start without a strong `JWT_SECRET`, a random DB password, secure cookies, explicit https `CORS_ORIGINS` and
  `FRONTEND_URL`, and `EMAIL_BACKEND=ses` (full list: [../docs/configuration.md](../docs/configuration.md#production-validation)).

## Authorization

Roles are `admin` and `user` (legacy member/designer/evaluator values were normalised to `user` by migration `0013`). The role is never accepted from signup or `PATCH /me`.
Admins manage accounts (`/admin/users`: create, edit, password, deactivate / reactivate, delete = anonymise, last-admin guard, session revocation) but have **no bypass** to other
people's charts or chats. Every endpoint that takes an id loads the resource through `app/authz.py` and answers **404** for anything the caller cannot see (403 `owner_only` only for
collaborators attempting owner actions). Charts follow `scorecards.owner_id` or an accepted collaborator row; evaluations and batches follow their chart; chats are private to their user
(plus read-only shares). Matrix and sharing rules: [../docs/plan-sharing-rbac.md](../docs/plan-sharing-rbac.md).

Other invariants worth knowing: a trashed chart (`deleted_at`) is invisible on every path except the owner's trash endpoints; each user has one running evaluation job and one running chat
turn (`409 user_job_active` / `user_chat_active`, enforced under per-user advisory locks); `PATCH` honours `If-Match` (`409 stale_edit`).

## Weight-sum trigger and the API

The "leaf weights of a version sum to 100" rule is a *deferred* constraint trigger that fires at `COMMIT`, and each request is its own transaction. Create a full sibling group
with the bulk endpoints (`POST /scorecard-versions/{id}/kpi-nodes/bulk`, `PATCH /kpi-nodes/weights`), not one node at a time; see the docstring in `app/api/v1/kpi_nodes.py`.

## Migrations

`python -m alembic upgrade head` works for ad-hoc use, but the supported path is `python -m app.scripts.migrate` (also creates the LangGraph checkpoint tables), run **once per
release** before the API and workers start. Migrations 0011 to 0016 added leases / idempotency keys, auth and ownership, sharing and RBAC, chat shares, chart trash, and
`evaluations.cancel_reason`. Rows created before login existed (such as the seed script's demo owner) have no password: claim one with `set_password`; everyone else signs up or is created by an admin.

## Tests

The suite (~900 tests, 80 files) runs against a real Postgres and never uses the dev database: `tests/conftest.py` forces `DATABASE_URL` to `TEST_DATABASE_URL`, or, when that is unset, to
`quality_scorecard_test` on the same instance, and `TRUNCATE`s tables between tests. AI paths use scripted fake Bedrock / Jev / web-search / AWS clients (`tests/fakes.py`), so no cloud
credentials are needed. conftest also clears `ENV`, `ADMIN_*`, `BOOTSTRAP_TOKEN`, `SIGNUP_ENABLED` and signs JWTs with a random per-session key.

```bash
# one-time (already done by infra/db-init on a fresh compose volume): create the test DB with both extensions
psql "postgresql://qs_app:<password>@127.0.0.1:5432/quality_scorecard" -c "CREATE DATABASE quality_scorecard_test OWNER qs_app;"
psql "postgresql://qs_app:<password>@127.0.0.1:5432/quality_scorecard_test" -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS ltree;"

cd backend
export TEST_DATABASE_URL="postgresql+psycopg://qs_app:<password>@127.0.0.1:5432/quality_scorecard_test"   # be explicit
export TEST_REDIS_URL="redis://127.0.0.1:6379/0"      # optional; without it the live Redis limiter tests are skipped
python -m pytest                                       # CI adds: --timeout=180 --timeout-method=thread
python -m ruff check .                                 # lint (CI runs this too)
```

A throwaway Redis for `TEST_REDIS_URL`: `docker run --rm -p 6379:6379 redis:7-alpine`. Sanity check that dev data is untouched: `GET /api/v1/scorecards` as your user before and after.
CI (`.github/workflows/backend-ci.yml`): Postgres `pgvector/pgvector:pg17` and Redis services, `alembic upgrade head`, LangGraph checkpoint warm-up, `pytest`, `ruff`.
The Lambda workers have their own suite: `cd ../aws/lambdas && python -m pytest`.

## Interactive API docs

`/docs`, `/redoc` and `/openapi.json` exist only outside production (`DOCS_ENABLED`). The static route table is in [../docs/api-reference.md](../docs/api-reference.md).
