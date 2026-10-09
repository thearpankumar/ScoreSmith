# Configuration reference

Every setting of the backend (`backend/app/config.py`, class `Settings`) plus the variables that only docker compose / the
frontend read. Names are the environment variable names (case-insensitive for pydantic-settings; compose passes them upper-case).
Template with comments: [`infra/.env.example`](../infra/.env.example). **Never commit `infra/.env`** (it is git-ignored).

How values reach the containers: `infra/docker-compose.yml` loads `infra/.env` into `migrate`, `backend` (api) and `worker`
(`env_file`) and then overrides a few keys (`DATABASE_URL`, `REDIS_URL`, `ROLE`, `ENV`, `FRONTEND_URL`, `CORS_ORIGINS`,
`FORWARDED_ALLOW_IPS`, `LOG_FORMAT`, `DB_POOL_*`, `AWS_BEARER_TOKEN_BEDROCK`). The `frontend` container gets **no** env_file, only
`INTERNAL_API_BASE_URL`.

"Required" below means required to *start the dev stack*; production adds the checks listed in [Production validation](#production-validation).

## Compose and infrastructure

| Name | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRES_PASSWORD` | yes | none | Postgres password. Compose refuses to start without it. Postgres only reads it when the data volume is first created |
| `POSTGRES_USER` / `POSTGRES_DB` | no | `qs_app` / `quality_scorecard` | Database user and name; also used to build `DATABASE_URL` for the containers |
| `POSTGRES_PORT` | no | `5432` | Host port (bound to `127.0.0.1` only) |
| `BACKEND_PORT` | no | `8000` | Host port of the API (bound to `127.0.0.1` only) |
| `FRONTEND_PORT` | no | `3000` | Host port of the Next.js app |
| `INTERNAL_API_BASE_URL` | no | `http://backend:8000` | Where the Next.js server (proxy, middleware, Server Components) reaches the API. Use `http://localhost:8000` for `npm run dev` on the host. Falls back to `NEXT_PUBLIC_API_BASE_URL`, then `http://localhost:8000` |
| `NEXT_PUBLIC_API_BASE_URL` | no | unset | Legacy fallback for the backend URL (not set by compose; the browser always uses the same origin) |
| `TEST_DATABASE_URL` | no | derived | Database used by `pytest` (see [backend/README.md](../backend/README.md#tests)). Default: `DATABASE_URL` with the database swapped for `quality_scorecard_test` |
| `TEST_REDIS_URL` | no | unset | Enables the live Redis limiter tests (skipped when unset or unreachable) |
| `FORWARDED_ALLOW_IPS` | no | private ranges + `127.0.0.1` | Proxies whose `X-Forwarded-For` gunicorn/uvicorn trust, so rate limits and the audit log see the real client. Set to your load balancer's addresses in production |
| `WEB_CONCURRENCY` | no | `2` | gunicorn worker processes per API container (read by the image `CMD`) |

## Core, process role, database, Redis

| Name | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | empty | `postgresql+psycopg://user:pass@host:port/db` (psycopg v3). Compose builds it from `POSTGRES_*` |
| `ENV` (alias `ENVIRONMENT`) | no | `development` | `production` / `prod` turns on the strict startup checks, secure cookies, HSTS, hides `/docs`, turns signup off, ignores `ADMIN_*` |
| `ROLE` | no | `all` | `api` (HTTP only), `worker` (`python -m app.worker`), `all` (both; local dev and tests). Compose sets it per service |
| `CORS_ORIGINS` | prod | `http://localhost:3000` | Comma-separated allowed browser origins. Production needs explicit https origins |
| `LOG_FORMAT` | no | `text` (compose: `json`) | `json` = one JSON object per line with `evaluation_id` / `session_id` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | no | `5` / `10` | Per process; keep `(size + overflow) x processes` under Postgres `max_connections` |
| `DB_POOL_RECYCLE_SECONDS` / `DB_POOL_TIMEOUT_SECONDS` | no | `1800` / `30` | Connection recycle and checkout timeout |
| `REDIS_URL` | no | empty (compose: `redis://redis:6379/0`) | Cluster-wide LLM concurrency and shared rate-limit counters. Empty = per-process only. If set but unreachable, everything **fails open** to per-process limits |
| `BEDROCK_GLOBAL_CONCURRENCY` | no | `0` | Cluster-wide cap on Bedrock calls; `0` = use `BEDROCK_MAX_CONCURRENCY` per process |
| `JEV_GLOBAL_CONCURRENCY` | no | `0` | Cluster-wide cap on Jev calls; `0` = uncapped |
| `WEB_SEARCH_GLOBAL_CONCURRENCY` | no | `0` | Cluster-wide cap on web-search calls; `0` = the client's own cap |
| `REDIS_SLOT_LEASE_SECONDS` | no | `600` | A slot a crashed process never released frees itself after this long |
| `REDIS_RETRY_AFTER_SECONDS` | no | `10` | After a Redis error, skip Redis for this long before probing again |
| `DOCS_ENABLED` | no | on outside production | `/docs`, `/redoc`, `/openapi.json`. Must be off in production |

## Leases, workers, autoscaling, trash

| Name | Required | Default | Purpose |
|---|---|---|---|
| `LEASE_SECONDS` | no | `60` | How long a claim on an evaluation / chat turn lasts without a heartbeat |
| `LEASE_HEARTBEAT_SECONDS` | no | `15` | Heartbeat interval (must be well below the lease) |
| `CHAT_SUPERVISOR_SECONDS` | no | `2` | How often a worker checks running chat turns for cancel / deleted session |
| `CHAT_TURN_MAX_CONCURRENT` | no | `8` | Chat turns one worker runs at once |
| `WORKER_HEALTH_PORT` | no | `8001` | Worker `/health`, `/ready`, `/metrics` |
| `WORKER_DRAIN_GRACE_SECONDS` | no | `20` | On SIGTERM, how long running chat turns may finish (compose `stop_grace_period` is 60 s) |
| `AUTOSCALE_METRIC_NAMESPACE` | no | empty | If set, workers publish `BacklogPerWorker` to CloudWatch under this namespace (always logged) |
| `AUTOSCALE_METRIC_INTERVAL_SECONDS` | no | `60` | Publish interval |
| `TRASH_RETENTION_DAYS` | no | `30` | Days a chart stays in the owner's trash before the housekeeping purge deletes it |

## Authentication, sessions, passwords

| Name | Required | Default | Purpose |
|---|---|---|---|
| `JWT_SECRET` | yes | none | HS256 key for the access JWT. Compose fails without it. Outside production an unset value becomes an *ephemeral* random key (sessions die on restart, processes disagree). Production: 32+ random characters |
| `JWT_ALGORITHM` | no | `HS256` | Signing algorithm |
| `ACCESS_TOKEN_MINUTES` | no | `15` | Access token lifetime |
| `REFRESH_DAYS_REMEMBER` | no | `30` | Refresh lifetime (and cookie lifetime) with "remember me" |
| `REFRESH_HOURS_SESSION` | no | `24` | Server-side refresh lifetime without "remember me" (browser session cookie) |
| `REFRESH_REUSE_GRACE_SECONDS` | no | `10` | Two tabs refreshing with the same token within this window are not treated as theft |
| `COOKIE_SECURE` | no | on in production | `Secure` flag. Must not be disabled in production |
| `COOKIE_SAMESITE` | no | `lax` | Cookie SameSite |
| `COOKIE_DOMAIN` | no | empty | Cookie domain (host-only when empty) |
| `FRONTEND_URL` | prod | `http://localhost:3000` | Public URL of the web app: links in e-mails and the CSRF `Origin` check |
| `PASSWORD_MIN_LENGTH` / `PASSWORD_MAX_LENGTH` | no | `12` / `128` | Password policy |
| `ADMIN_PASSWORD_MIN_LENGTH` | no | `14` | Stricter minimum for admin accounts |
| `LOCKOUT_THRESHOLD` | no | `5` | Failed logins before the account locks |
| `LOCKOUT_BASE_MINUTES` / `LOCKOUT_MAX_MINUTES` | no | `5` / `60` | First lock duration; doubles per further failure up to the max |
| `PASSWORD_RESET_TTL_MINUTES` / `EMAIL_VERIFY_TTL_HOURS` | no | `60` / `48` | Token lifetimes |
| `SIGNUP_ENABLED` | no | on in dev, **off** in production | Public self-service signup and OAuth account creation |
| `EMAIL_BACKEND` | prod | `log` | `log` writes links to the server log (dev); `ses` sends through Amazon SES. Production refuses `log` |
| `EMAIL_FROM` | with `ses` | empty | Sender address |
| `OAUTH_REDIRECT_BASE` | no | `http://localhost:3000` | Browser-facing base URL for OAuth redirect URIs (`.../api/v1/auth/oauth/{provider}/callback`) |
| `OAUTH_GOOGLE_CLIENT_ID` / `_SECRET`, `OAUTH_GITHUB_CLIENT_ID` / `_SECRET`, `OAUTH_MICROSOFT_CLIENT_ID` / `_SECRET` | no | empty | A provider is enabled only when both of its values are set; otherwise its button is disabled and `/start` answers 501 |

## First admin and provisioning

| Name | Required | Default | Purpose |
|---|---|---|---|
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | dev only | empty | Non-production: api and worker create this admin on startup if no user has the email (never changes an existing user). **Ignored with a warning in production** |
| `ADMIN_USERNAME` | no | `admin` | Non-production: signing in as this name resolves to the `ADMIN_EMAIL` account |
| `BOOTSTRAP_TOKEN` | prod first run | empty | 32+ random characters. `POST /auth/register-user` with `X-Bootstrap-Token` creates the first admin while zero users exist; remove it afterwards |

## Rate limits (`count/period`, the `limits` syntax)

| Name | Default | Scope |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Master switch |
| `RATE_LIMIT_LOGIN` | `10/minute` | per IP, and per address |
| `RATE_LIMIT_SIGNUP` | `5/minute` | per IP |
| `RATE_LIMIT_FORGOT` / `RATE_LIMIT_RESET` | `5/hour` / `10/hour` | per IP |
| `RATE_LIMIT_REFRESH` | `60/minute` | per IP |
| `RATE_LIMIT_REGISTER_USER` | `5/minute` | per IP |
| `RATE_LIMIT_CHAT` | `30/minute` | per user |
| `RATE_LIMIT_AI_JOBS` | `20/minute` | per user |
| `RATE_LIMIT_UPLOADS` | `30/minute` | per user |
| `RATE_LIMIT_EXPORT` | `10/minute` | per user (export and bulk delete) |
| `RATE_LIMIT_SHARE_LOOKUP` | `20/hour` | per *sender*: every invite / share attempt, bounds username probing |
| `RATE_LIMIT_SHARE` | `60/minute` | per user |
| `RATE_LIMIT_ADMIN` | `60/minute` | per admin |

## LLM providers

| Name | Required | Default | Purpose |
|---|---|---|---|
| `AWS_REGION` | no | `us-east-1` | Region for Bedrock, S3, Step Functions |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | for AI | empty | Bedrock credential. In this project they are a Bedrock long-term *API key*: `docker-compose.yml` derives `AWS_BEARER_TOKEN_BEDROCK: A${AWS_SECRET_ACCESS_KEY}` (botocore prefers it for Bedrock). A real SigV4 key pair also works if you remove that line |
| `BEDROCK_CHAT_MODEL_ID` | no | `zai.glm-5` | Chat / scorecard builder model |
| `BEDROCK_JUDGE_MODEL_ID` | no | `zai.glm-4.7-flash` | Ensemble judge model |
| `BEDROCK_EMBEDDING_MODEL_ID` | no | `amazon.titan-embed-text-v2:0` | Embeddings for similarity search |
| `BEDROCK_MASTER_MODEL_ID` | no | `us.meta.llama4-maverick-17b-instruct-v1:0` | Master agent of the AI evaluation pipeline |
| `BEDROCK_MAX_OUTPUT_TOKENS` | no | `16000` | Output ceiling per call (`0` omits it) |
| `BEDROCK_MAX_CONCURRENCY` | no | `8` | Max simultaneous Bedrock calls per process |
| `BEDROCK_READ_TIMEOUT_SECONDS` / `BEDROCK_CONNECT_TIMEOUT_SECONDS` | no | `240` / `10` | boto3 client timeouts |
| `BEDROCK_MAX_ATTEMPTS` / `BEDROCK_MAX_POOL_CONNECTIONS` | no | `2` / `32` | boto3 retries and pool size |
| `OPENROUTER_JEV_API` | no | empty | OpenRouter key for Jev (quality gate, mode router, per-KPI scoring). Empty: gates degrade to "passed"; evaluation scoring falls back to a model judgment flagged `needs_review` |
| `OPENROUTER_JEV_MODEL_ID` | no | `typesafe/jev-1.13` | Pinned Jev model |
| `REQUEST_ROUTER` | no | `auto` | `auto` / `jev` / `small_model` / `off`: cheap intent router in front of the full extraction |
| `AGENTCORE_GATEWAY_WEB_SEARCH_URL`, `_TOOL_NAME`, `AGENTCORE_GATEWAY_AWS_ACCESS_KEY_ID`, `AGENTCORE_GATEWAY_AWS_SECRET_ACCESS_KEY` | no | empty | Web search for the research fan-out (a separate IAM principal). Any unset: web search returns no results, turns still succeed |

Provider split in one line: chat, judge, master agent and embeddings use **Bedrock** (bearer token above); Jev uses **OpenRouter**;
S3 and Step Functions use the dedicated `AWS_APP_*` keys below, never the Bedrock token.

## Chat builder tuning

| Name | Default | Purpose |
|---|---|---|
| `CHAT_TURN_TIMEOUT_SECONDS` | `900` | Wall-clock limit of one background turn |
| `CHAT_TURNS_INLINE` | `false` | Test-only: run turns inside the request |
| `USER_KPIS_PER_FILL_CALL` | `5` | KPIs per guideline-writing call (user-specified mode) |
| `USER_RESEARCH_TIMEOUT_SECONDS` | `45` | Budget of the bounded research over the user's KPIs |
| `USER_ENRICH_DEADLINE_SECONDS` | `170` | Budget of the whole enrichment phase |
| `OPEN_FILL_DEADLINE_SECONDS` | `150` | Budget of one category's guideline fan-out |
| `QUALITY_GATE_CALL_TIMEOUT_SECONDS` | `12` | Per-Jev-call timeout (a slow call counts as "passed") |
| `QUALITY_GATE_BUDGET_SECONDS` | `150` | Per-turn quality-gate time budget |

## AI evaluation pipeline

| Name | Required | Default | Purpose |
|---|---|---|---|
| `S3_BUCKET` / `SFN_STATE_MACHINE_ARN` | for file / Drive evaluations | empty | Written to `infra/.env` by `aws/bootstrap`. Without them the flow answers 503 |
| `AWS_APP_ACCESS_KEY_ID` / `AWS_APP_SECRET_ACCESS_KEY` | for file / Drive evaluations | empty | Least-privilege IAM user `qs-backend-app` (S3 prefixes + one state machine) |
| `AI_EVAL_MAX_CONCURRENT` | no | `3` | Evaluations processed at once cluster-wide (clamped 1-5) |
| `AI_EVAL_POLL_SECONDS` | no | `4` | Dispatcher poll interval |
| `AI_EVAL_TIMEOUT_SECONDS` | no | `14400` | Wall-clock ceiling per evaluation |
| `AI_EVAL_SCORING_RETRIES` | no | `2` | Automatic scoring re-runs after a transient failure |
| `AI_EVAL_SCORING_RETRY_DELAYS` | no | `20,60` seconds | Waits between those retries (JSON list in env, e.g. `[20,60]`) |
| `AI_EVAL_INLINE` | no | `false` | Test-only: no background dispatcher loop |
| `UPLOAD_MAX_BYTES` / `UPLOAD_MAX_FILES` | no | 2 GiB / `10` | Per file / per submission |
| `UPLOAD_PART_SIZE` / `UPLOAD_URL_TTL_SECONDS` | no | 32 MiB / `900` | Multipart part size and presigned URL lifetime |
| `UPLOAD_USER_DAILY_BYTES` | no | 20 GiB | Per-user rolling 24 h upload quota |

## Production validation

With `ENV=production` the api **and** worker refuse to start (`RuntimeError: Unsafe production configuration: ...`) unless:

- `JWT_SECRET` is 32+ characters and not a placeholder (`change_me`, `example`, `password`, ...);
- `DATABASE_URL` has a random password of 12+ characters;
- `COOKIE_SECURE` is not disabled and `DOCS_ENABLED` is off;
- `CORS_ORIGINS` is set explicitly, https only (no `*`, no localhost);
- `FRONTEND_URL` is set explicitly and is not localhost;
- `EMAIL_BACKEND` is not `log`;
- `BOOTSTRAP_TOKEN`, if set, is 32+ characters.

`ADMIN_EMAIL` / `ADMIN_PASSWORD` set in production only log a warning and are ignored.
