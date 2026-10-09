# infra: docker compose stack

`docker-compose.yml` runs the whole product locally (and is a good template for a single-host deployment). Nothing secret lives in the
repo: copy `.env.example` to `.env` (git-ignored) and fill in your own values. The complete list of variables is in
[../docs/configuration.md](../docs/configuration.md); architecture diagrams are in [../docs/architecture.md](../docs/architecture.md).

```bash
cd infra
cp .env.example .env        # then set POSTGRES_PASSWORD, JWT_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD (+ AWS / OpenRouter keys for AI)
docker compose config --quiet   # validates the file and that required variables are set
docker compose up --build -d
docker compose ps
```

Generate strong values: `python -c "import secrets; print(secrets.token_urlsafe(32))"` (database password, URL-safe characters only) and
`token_urlsafe(48)` for `JWT_SECRET` / `BOOTSTRAP_TOKEN`. Compose refuses to start while `POSTGRES_PASSWORD` or `JWT_SECRET` is empty.

## Services

| Service | Image / build | Host port | Role |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg17` | `127.0.0.1:${POSTGRES_PORT:-5432}` | Postgres 17 with `vector` + `ltree`. Healthcheck `pg_isready` |
| `redis` | `redis:7-alpine` (no persistence) | none (compose network only) | Shared Bedrock / Jev / web-search limits and rate-limit counters. Optional at runtime: everything fails open without it |
| `migrate` | `../backend` image | none | One-off job: `python -m app.scripts.migrate` (Alembic to head, then LangGraph checkpoint tables). `restart: "no"`. `backend` and `worker` wait for it to complete |
| `backend` | `../backend` image, `ROLE=api` | `127.0.0.1:${BACKEND_PORT:-8000}` | gunicorn + uvicorn workers (`WEB_CONCURRENCY`, default 2). HTTP only; queues long jobs in Postgres. Healthcheck `GET /ready` |
| `worker` | same image, `ROLE=worker`, `python -m app.worker` | none | Evaluation dispatcher + background chat turns. Health server on 8001 (`/health`, `/ready`, `/metrics`). `stop_grace_period: 60s` for the SIGTERM drain |
| `frontend` | `../frontend` image (`npm run dev`) | `${FRONTEND_PORT:-3000}` | Next.js. Proxies `/api/v1/*` to `INTERNAL_API_BASE_URL` (`http://backend:8000`). Gets no env_file, so no backend secrets |

```
        host                          compose network
  :3000  --> frontend ----------------> backend:8000 --\
  :8000 (127.0.0.1) --> backend                          >--> db:5432 (also 127.0.0.1:5432 on the host)
  :5432 (127.0.0.1) --> db            worker (no port) --/--> redis:6379 (network only)
```

Volumes: `qs_pg_data` (named volume with all data). Bind mounts: `../backend:/app` for `migrate`, `backend` and `worker`; `../frontend:/app` for
`frontend` (with anonymous volumes for `/app/node_modules` and `/app/.next` so the image's installed dependencies are not shadowed).
`./db-init` is mounted into Postgres' `docker-entrypoint-initdb.d`: `02-create-test-db.sql` creates `quality_scorecard_test` for `pytest` on a fresh
volume only.

## First run

1. `docker compose up --build -d`: `db`, `redis` become healthy, `migrate` runs and exits 0, then `backend`, `worker`, `frontend` start.
2. Open <http://localhost:3000> (the public homepage), then **Sign in**. In development the first admin comes from `ADMIN_EMAIL` and
   `ADMIN_PASSWORD` in `.env` (created on startup if the email has no account yet). You can sign in with the e-mail, with `ADMIN_USERNAME`
   (default `admin`, development only) or with the part of the e-mail before the `@` if it is unique.
3. Optional demo data: `docker compose exec backend python -m app.scripts.seed`.

## Day-to-day commands

| Task | Command |
|---|---|
| Logs | `docker compose logs -f backend worker` |
| Apply new migrations only | `docker compose run --rm migrate` |
| Restart after editing backend code | `docker compose restart backend worker` (no `--reload`: long jobs must not be killed by a file change) |
| Frontend does not pick up edits (Windows / Docker Desktop) | `docker compose restart frontend` (file events from the host do not always reach the container; `WATCHPACK_POLLING=true` on the service is the alternative) |
| Rebuild after dependency changes | `docker compose up --build -d` |
| Worker health / backlog | `docker compose exec worker python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8001/metrics').read().decode())"` |
| Reset the password of an account | `docker compose exec backend python -m app.scripts.set_password <email>` (prompts) |
| psql | `docker compose exec db psql -U qs_app -d quality_scorecard` |

Admin CLIs (flags and behaviour in [../backend/README.md](../backend/README.md#command-line-tools)): `set_password`, `share_everything`, `purge_users`, `seed`.

## Scaling the workers

The compose file runs **one** worker (`deploy.replicas: 1`). Add more only on purpose; `--scale` wins over the file:

```bash
docker compose up -d --scale worker=3
```

- Workers coordinate only through Postgres (leases, `SKIP LOCKED`) and Redis (shared limits); nothing else to configure.
- The global evaluation cap `AI_EVAL_MAX_CONCURRENT` (1-5) applies to the whole cluster, not per worker. Chat turns: `CHAT_TURN_MAX_CONCURRENT` per worker.
- Each user still has at most one running evaluation job and one running chat turn.
- Scale-in is safe: a worker that gets SIGTERM drains and releases its leases; a killed worker's rows are adopted once the lease (60 s) expires.
- Signal for an autoscaler: `backlog_per_worker = (queued + active evaluations + waiting chat turns) / workers`, from the worker's `/metrics` or, with
  `AUTOSCALE_METRIC_NAMESPACE` set, the CloudWatch metric `BacklogPerWorker`.
- API replicas: `docker compose up -d --scale backend=2` needs the host port mapping removed (or put a load balancer in front); in production run the API behind
  a TLS-terminating proxy and set `FORWARDED_ALLOW_IPS` to its addresses.

## Production notes

This compose file is a **development** setup (bind mounts, `next dev`, loopback ports, plaintext `.env`). For a production host or AWS use
[../docs/cloud_deployment_plan.md](../docs/cloud_deployment_plan.md) and the checklist in the [root README](../README.md#production-checklist). Short version:
`ENV=production`, strong `JWT_SECRET` / DB password, https `CORS_ORIGINS` and `FRONTEND_URL`, `EMAIL_BACKEND=ses`, `BOOTSTRAP_TOKEN` for the first admin (then remove it),
`FORWARDED_ALLOW_IPS` for the proxy, a production Next.js build instead of `npm run dev`.

## Backups and restore

All state (including LangGraph checkpoints) is in the one Postgres database. Redis holds nothing worth keeping; S3 objects follow their own lifecycle rules.

```bash
# Backup (custom format, compressed). Written inside the container, then copied out: avoids shell-redirect problems on Windows.
docker compose exec db pg_dump -U qs_app -d quality_scorecard -Fc -f /tmp/qs.dump
docker compose cp db:/tmp/qs.dump ./qs-$(date +%Y%m%d).dump

# Restore into an empty database (stop writers first)
docker compose stop backend worker frontend
docker compose cp ./qs-20260101.dump db:/tmp/qs.dump
docker compose exec db psql -U qs_app -d postgres -c "DROP DATABASE IF EXISTS quality_scorecard" -c "CREATE DATABASE quality_scorecard OWNER qs_app"
docker compose exec db pg_restore -U qs_app -d quality_scorecard --no-owner /tmp/qs.dump
docker compose run --rm migrate          # no-op when the dump is current, upgrades an older dump
docker compose start backend worker frontend
```

PowerShell: replace `$(date +%Y%m%d)` with `$(Get-Date -Format yyyyMMdd)`. The extensions `vector` and `ltree` are recreated by the dump. For production use managed
backups (Aurora automated backups / point-in-time recovery) instead; see the cloud plan.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Set POSTGRES_PASSWORD in infra/.env` / `Set JWT_SECRET ...` | Required variables missing; fill them in `.env` |
| `password authentication failed` after changing `POSTGRES_PASSWORD` | Postgres only reads it when the volume is first created; put the original value back or `ALTER USER qs_app PASSWORD '...'` inside the db container |
| `backend` waits forever | `migrate` failed: `docker compose logs migrate` |
| Chat or evaluation stays queued | No worker running: `docker compose ps worker`; start it with `docker compose up -d worker` |
| Redis down | Harmless: limits fall back to per-process and rate-limit counters to in-memory; `/ready` reports `redis: down` |
| `tsc` / `next build` reports errors about routes or files that no longer exist | Stale generated types: `tsconfig.json` includes `.next/types/**`. Delete them and re-run: `docker compose exec frontend rm -rf .next/types` (or `rm -rf frontend/.next/types` on the host) |
