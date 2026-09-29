# Cloud Deployment Readiness Plan — Quality Scorecard System

Status: **planning only — nothing in this document has been implemented.** It is a concrete, executable checklist for moving the current local Docker Compose stack (`infra/docker-compose.yml`) to a real AWS deployment, written against the actual current stack (Postgres 17 + pgvector/ltree, FastAPI backend, Next.js frontend, AWS Bedrock + AgentCore Gateway credentials in a local `.env`).

## 1. Current state (what we're migrating away from)

| Component | Today | Gap for cloud |
|---|---|---|
| Database | `pgvector/pgvector:pg17` container, single Compose volume `qs_pg_data`, no backups, no HA | No managed backups/PITR, no failover, single point of failure |
| Backend | FastAPI in a `python:3.12-slim` container, bind-mounted source (`../backend:/app`), `--reload` uvicorn | Dev-mode container; no image built for prod, no health-checked orchestration, no autoscaling |
| Frontend | Next.js in a Node container, bind-mounted source + anonymous volumes for `node_modules`/`.next` | Same — dev-mode container, not a production Next.js build/image |
| Secrets | Plaintext `infra/.env`, `env_file:` in Compose, real AWS Bedrock keys and AgentCore Gateway keys committed to a local file (gitignored, but still plaintext on disk) | No secrets manager, no rotation, no least-privilege separation between the two credential sets already noted in `app/config.py` (main Bedrock vs. AgentCore Gateway) |
| Networking | Host port mapping (`8000`, `3000`, `5432` all exposed to `localhost`) | No TLS, no domain, DB port should never be public in the cloud |
| CI/CD | None found in the repo | No automated build/test/deploy pipeline |

## 2. Database: Aurora PostgreSQL-Compatible migration

**Target:** Aurora PostgreSQL (Provisioned or Serverless v2), single writer + at least one reader for Multi-AZ failover.

### 2.1 Extension/version parity check (do this FIRST, before anything else)

The app uses exactly two non-default extensions, both created in `alembic/versions/0001_initial_schema.py`:
- `vector` (pgvector) — used for `scorecard_embeddings` (1024-dim Titan embeddings, cosine similarity).
- `ltree` — used for `kpi_nodes.path` (hierarchical KPI tree, via `sqlalchemy_utils.LtreeType`).

Confirmed via research for this plan (2026): Aurora PostgreSQL supports `pgvector` 0.8.x on Aurora PostgreSQL 17.4+/16.8+/15.12+ (all commercial regions + GovCloud, not China) — this lines up cleanly with the local image already being `pg17`. `ltree` is a standard supported RDS/Aurora extension (it ships in PostgreSQL contrib and is on Aurora's allow-listed extension set) but **the exact `ltree` version available must be checked against the specific target Aurora PostgreSQL engine version before migration** — run `SELECT * FROM pg_available_extensions WHERE name IN ('vector','ltree');` against a real Aurora test cluster at the chosen engine version and confirm both are creatable, before scheduling any cutover. Do not assume; verify on the actual target engine version.

Action items:
1. Stand up a throwaway Aurora PostgreSQL 17.x cluster (smallest instance class, e.g. `db.r6g.large` provisioned, or Serverless v2 with a low max-ACU) in a sandbox VPC.
2. Run `alembic upgrade head` against it end-to-end (same migrations used locally — no SQL changes expected, since both extensions are already `CREATE EXTENSION IF NOT EXISTS`).
3. Run the full backend test suite (`pytest`, currently 69 tests) against that cluster via `DATABASE_URL`/`TEST_DATABASE_URL` pointed at it, to catch any Aurora-specific behavioral difference (e.g. `pg_isready`-equivalent healthcheck semantics, connection limits, `ltree` operator support at that exact version).
4. Only after (2) and (3) pass cleanly, proceed to real data migration planning (moot right now since the DB is currently empty by design, but this becomes real once real user data exists).

### 2.2 Migration path from the local container

- **Schema:** identical — Alembic migrations (`backend/alembic/versions/`) are the single source of truth already; they run unchanged against Aurora.
- **Data:** for a genuinely empty-to-low-volume cutover (current state), a `pg_dump`/`pg_restore` (or even a fresh `alembic upgrade head` + reseed via `app/scripts/seed.py` if only demo data is needed) is sufficient — no need for AWS DMS at this scale. If/when the app accumulates real production data before this migration happens, switch to **AWS DMS** (continuous replication, minimal-downtime cutover) instead of a one-shot dump/restore.
- **Connection string:** `DATABASE_URL` already uses the `postgresql+psycopg://` scheme the app expects (`app/config.py`); only the host/credentials change — the app-side connection code needs zero changes, only config.
- **pgvector index tuning:** re-verify HNSW/IVFFlat index parameters (if any are added later — none exist yet beyond the raw `vector` column) against Aurora's documented pgvector performance guidance, since Aurora's storage layer differs from a local Docker volume.

### 2.3 What changes vs. what doesn't

| Stays the same | Changes |
|---|---|
| SQLAlchemy models, Alembic migrations, `psycopg` v3 driver | Host/port/credentials (via Secrets Manager, not `.env`) |
| pgvector/ltree usage patterns in code | No more local Docker volume — Aurora storage + automated backups + PITR |
| `DATABASE_URL` env var shape | Add a read-replica-aware setup if/when read scaling is needed (not needed at current scale) |

## 3. Secrets Manager wiring (Bedrock + AgentCore Gateway credentials)

Today `infra/.env` holds, in plaintext: `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (actually a Bedrock long-term API key per the existing comments in `.env.example`), `BEDROCK_CHAT_MODEL_ID`/`BEDROCK_JUDGE_MODEL_ID`, and a **separate** AgentCore Gateway credential pair (`AGENTCORE_GATEWAY_AWS_ACCESS_KEY_ID`/`AGENTCORE_GATEWAY_AWS_SECRET_ACCESS_KEY`) plus the Gateway URL/tool name.

Plan:
1. Create two secrets in **AWS Secrets Manager** (keep them separate — they're already documented as a distinct, differently-scoped IAM principal in `app/config.py`'s comments, and mixing them defeats that separation):
   - `quality-scorecard/bedrock-credentials` — `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or, better: migrate off long-term keys entirely and use the ECS task's own IAM role for Bedrock — see 3.1 below).
   - `quality-scorecard/agentcore-gateway-credentials` — the Gateway-scoped key pair + URL + tool name.
2. Non-secret config (`BEDROCK_CHAT_MODEL_ID`, `BEDROCK_JUDGE_MODEL_ID`, `BEDROCK_EMBEDDING_MODEL_ID`, `AWS_REGION`) stays as plain **ECS task-definition environment variables** (or SSM Parameter Store `String` params) — no need to pay for Secrets Manager rotation/storage on non-secret values.
3. Wire secrets into the ECS task definition via `secrets:` (not `environment:`), referencing the Secrets Manager ARNs — ECS injects them as env vars at container start, so `app/config.py`'s existing `pydantic_settings` env-var reading needs **zero code changes**.
4. Rotate: set a 90-day rotation schedule on both secrets once real IAM users back them (Secrets Manager rotation Lambdas for IAM access keys are a standard AWS-provided template).

### 3.1 Better than a migrated `.env`: drop long-term keys for Bedrock entirely

Since the backend already talks to Bedrock via `boto3` with the SDK's normal credential resolution chain (`app/ai/bedrock_client.py`'s `boto3.client("bedrock-runtime", ...)`, no explicit credentials passed), the cleanest cloud migration is to **not** put Bedrock keys in Secrets Manager at all — instead attach an **ECS task IAM role** with a scoped `bedrock:InvokeModel`/`bedrock:Converse` policy (restricted to the specific `zai.glm-5` / `zai.glm-4.7-flash` / Titan model ARNs in use). This is strictly better than the current bearer-token workaround (`AWS_BEARER_TOKEN_BEDROCK`, documented in `.env.example` as a workaround for API-key-shaped credentials) and removes one whole secret from the system. The AgentCore Gateway credential is SigV4-signed by hand (`web_search.py`) rather than via boto3, so it should still go through Secrets Manager unless/until that code path is also moved to the task role + `botocore`'s SigV4 signer directly.

## 4. Container registry + hosting: recommendation

**Recommendation: AWS App Runner for both backend and frontend**, with a documented reassessment trigger to move the backend to ECS Fargate later if traffic becomes non-bursty/high-utilization.

Justification (grounded in a 2026 pricing/practice check, not guesswork): ECS+Fargate has a $0 control plane but ~40-60% higher steady-state compute cost than App Runner once you count the operational overhead of writing your own task definitions, target groups and autoscaling policies; App Runner costs more per vCPU/GB but has no per-request/traffic surcharge and is dramatically cheaper for a workload with significant idle time (App Runner scales toward zero when idle) — which is exactly this app's current traffic profile (an internal QA tool, not a high-QPS public service). EKS is not justified here at all: its ~$73/month control-plane fee plus operational complexity only pays for itself once you actually need Kubernetes-specific features (custom schedulers, multi-tenant cluster sharing, existing K8s tooling investment), none of which apply to this two-service app.

**Reassessment trigger:** if the backend's traffic becomes steady/high-utilization (not bursty) such that App Runner's per-vCPU premium starts costing more than ECS Fargate's operational overhead is worth avoiding, move the backend (not necessarily the frontend) to ECS Fargate behind an ALB. Revisit this decision explicitly once real usage data exists — don't guess.

Registry: **Amazon ECR**, one repository per service (`quality-scorecard/backend`, `quality-scorecard/frontend`), lifecycle policy to expire untagged images after 14 days and keep the last 10 tagged releases.

Dockerfile changes needed:
- Backend: current `Dockerfile` already does `pip install --no-cache-dir .` — the only change needed is dropping `--reload` from the `CMD` for the prod image (keep a separate `docker-compose.yml`-only dev entrypoint, or gate it on an `ENVIRONMENT` build arg). No bind mount in prod — the image is the deployable artifact.
- Frontend: needs a genuine **multi-stage production build** (`next build` + `next start`, or `output: "standalone"` for a smaller final image) — today's frontend `Dockerfile`/Compose setup is dev-mode only (bind-mounted source + `node_modules` anonymous volume). This is real, if modest, work: add a `next.config.js` `output: "standalone"` setting, a multi-stage `Dockerfile` (deps → build → runtime), and confirm `NEXT_PUBLIC_API_BASE_URL` is baked in at build time correctly (it's a build-time-inlined env var in Next.js, unlike `INTERNAL_API_BASE_URL` which is read at runtime in Server Components) — this distinction matters more once bind-mounting is gone.

## 5. TLS / domain

1. Register (or use an existing) domain in **Route 53**, or delegate a subdomain to it (e.g. `qs.<company-domain>.com`).
2. Request a public certificate in **ACM** (`us-east-1` if CloudFront is ever fronting this; otherwise same region as the App Runner/ALB resources) for `qs.<domain>` and `api.qs.<domain>`.
3. App Runner supports custom domains + managed TLS natively (auto-provisions and renews the ACM cert once domain ownership is verified via the CNAME/DNS records App Runner gives you) — no separate ALB/CloudFront needed unless the reassessment trigger in §4 moves the backend to ECS Fargate, in which case an ALB + ACM cert + Route 53 alias record is the standard pattern.
4. Frontend → backend calls: `NEXT_PUBLIC_API_BASE_URL` becomes `https://api.qs.<domain>` (build-time), and CORS (`cors_origins` in `app/config.py`) must be updated to the real frontend domain instead of `http://localhost:3000`.

## 6. CI/CD outline

No CI/CD exists in the repo today. Proposed pipeline (**GitHub Actions**, since the repo already looks GitHub-hosted; swap for CodePipeline if AWS-native tooling is preferred):

1. **On PR**: `ruff check .` + `pytest` (backend), `tsc --noEmit` + `npm run lint` + `npm run build` (frontend) — all four already exist as commands in this repo (see Part 2 of the accompanying work for exact invocations), just need wiring into a workflow file. No deploy on PR.
2. **On merge to `main`**:
   - Build both Docker images, tag with the git SHA, push to ECR.
   - Run `alembic upgrade head` against the target Aurora cluster as a one-off ECS task (or App Runner doesn't run migrations itself — a separate `aws ecs run-task` / CodeBuild step is the standard pattern for "run migrations before/during deploy").
   - Deploy: `aws apprunner start-deployment` for both services (or update the App Runner service's image tag via CLI/CDK/Terraform, which is what should actually own these resources — see §7).
3. **Rollback:** App Runner keeps the previous deployment's image; a failed health check auto-rolls-back by default. Alembic migrations should be written additive-only (the existing `0001`/`0002` migrations already follow this pattern — no destructive column drops in `upgrade()`) so a rollback of the app image never leaves the DB in an incompatible state.

## 7. Infrastructure as Code

None exists today (`infra/docker-compose.yml` is the only infra definition, and it's Compose, not cloud IaC). Recommend **Terraform** (or AWS CDK if the team prefers writing infra in Python/TypeScript to match the app stack) to define: the Aurora cluster, ECR repos, App Runner services, Secrets Manager secrets, IAM roles/policies (task role scoped to Bedrock + Secrets Manager read + CloudWatch Logs), Route 53 records, and ACM certs — as actual version-controlled resources, not click-ops. This is real, non-trivial work (estimate: 2-4 days for a first pass covering everything in this document) and should be scoped as its own follow-up task, not bundled into a "quick" deploy.

## 8. What changes in `docker-compose.yml` / env handling to get there

Concretely, moving off local Compose means:

1. `docker-compose.yml` stops being the deployment artifact — it remains **local-dev-only** (this is fine and normal; keep it as-is for local development, don't try to make one file serve both purposes).
2. Two new production Dockerfiles (or `ENVIRONMENT`-gated build stages in the existing ones): backend drops `--reload`; frontend gets a real multi-stage prod build (`output: "standalone"`).
3. `app/config.py`'s `Settings` class needs **no changes** — it already reads everything from env vars via `pydantic_settings`, which is exactly what ECS `environment:`/`secrets:` injection provides. This is the main reason the current design travels well to the cloud with minimal code churn.
4. `infra/.env` / `infra/.env.example` stop being the source of truth for anything beyond local dev — production values live in Secrets Manager + the ECS/App Runner task definition's plain env vars (§3), and `.env.example` continues to document what those values *are* for local onboarding.
5. The DB connection string changes from `db:5432` (Compose service DNS) to the Aurora cluster endpoint — again, just a config value, no code change, since `DATABASE_URL` is already externalized.
6. CORS origins (`cors_origins` default `http://localhost:3000`) must be set to the real frontend domain via env var override in the ECS/App Runner task config.
7. AgentCore Gateway's hand-signed SigV4 requests (`web_search.py`) are unaffected by any of the above — they already read their four config values from `Settings`, so only the secret source changes, not the code.

## 9. Sequencing (do this in order)

1. Aurora extension/version parity check (§2.1) — cheap, fast, de-risks everything else.
2. Terraform/CDK skeleton for Aurora + ECR + Secrets Manager (§7), stood up in a sandbox account first.
3. Production Dockerfiles for both services (§4).
4. CI pipeline for build/test (no deploy yet) (§6.1).
5. Deploy to sandbox App Runner services against sandbox Aurora, full smoke test (create scorecard, run evaluation, chat flow — the same three flows exercised in this task's Part 1 live verification).
6. TLS/domain (§5), CI deploy stage (§6.2-6.3), then cut real traffic over.

Everything above is scoped to be executed by a small team incrementally; nothing here requires a big-bang cutover.
