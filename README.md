# ScoreSmith

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.7-3178C6?logo=typescript&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-%2B%20ltree-4169E1?logo=postgresql&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4-06B6D4?logo=tailwindcss&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C)
![AWS Bedrock](https://img.shields.io/badge/AWS-Bedrock-232F3E?logo=amazonwebservices&logoColor=white)
![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

**ScoreSmith** (working title: Quality Scorecard System) is a generic scorecard creation & rating system — think Google Forms, but for quality scorecards. Instead of hard-coding one fixed rubric for one fixed type of work, ScoreSmith lets a user *define* what quality means for any domain (a weighted, hierarchical set of KPIs with 11-level qualitative + quantitative guidelines) through a chat-driven, research-grounded builder, then apply that definition consistently and repeatably — by a human or an ensemble LLM judge — to rate real inputs and get a reasoned, evidence-cited, RAG-banded score. It is built directly against TalenciaGlobal's Quality Scorecard Framework and Data-Driven Development Framework (`References/`), and the current build is explicitly scoped to Phase 0 + Cycle 1 of that framework (see [Status & roadmap](#status--roadmap)).

## Key features

- **Chat-driven scorecard builder** — a [LangGraph](https://github.com/langchain-ai/langgraph) state machine, backed by AWS Bedrock (Z.ai GLM-5), asks clarifying questions, proposes KPIs "LLM first, human second," and checkpoints every step to Postgres so a session survives a server restart mid-question.
- **Genuine multi-agent research fan-out** — before the first KPI is ever proposed, the graph decides 0–4 distinct research angles for the user's domain, then runs that many independent research agents *concurrently* (`asyncio.gather`, not sequentially), each with its own real web-search budget via an AWS Bedrock AgentCore Gateway MCP tool. Each agent proposes its **own** small batch of fully-specified, grounded KPIs (name, weight, full 11-level guidelines) — the total KPI count grows with research breadth instead of being capped by one giant end-of-pipeline call.
- **Embedding-based reuse suggestions** — a new request is checked against existing scorecards (pgvector cosine similarity over Titan-embedded purpose text, threshold 0.75) and offered as "use as-is / adapt / start fresh" *before* generating from scratch.
- **Ensemble LLM judge** — every leaf KPI is scored by **3 independent** Bedrock Converse calls (Z.ai GLM-4.7-Flash) with the guideline rungs presented in a different order each time (ascending / descending / deterministic shuffle) to counter position bias, aggregated by median, and flagged `needs_review` when the calls disagree.
- **Reasoning-before-score judging** — the judge's tool schema forces `matched_level → evidence_quotes → reasoning → score`, so the model commits to a rubric level and cites verbatim evidence before it ever writes a number.
- **Custom scoring-formula engine** — the default weighted average can be overridden with an arbitrary safe expression (`kpi["Name"] * 0.6 + min(kpi["A"], kpi["B"]) * 0.4`, `+ - * / **`, `min/max/avg/mean/sqrt/abs`), parsed via `simpleeval`'s AST whitelist (no `eval`), used identically by both the AI judge and manual scoring paths so they can never disagree about what a score means.
- **Hierarchical KPI trees** — up to 4 levels deep, stored as Postgres `ltree` materialized paths, each leaf carrying a full 0–10 qualitative + quantitative guideline ladder.
- **Live agent-execution trace** — every research agent and the orchestrator itself streams granular events (`started`, `searching`, `search_result`, `proposing_kpis`, `completed`, `quality_gate`/`quality_gate_retry`, ...) to a dedicated table, rendered live in the chat UI instead of a generic spinner.
- **Quality-gate self-critique layer** — three checkpoints in the chat pipeline (the master's research-angle plan, each research agent's finding, and `propose_kpis`'s final per-turn answer) are rated 0–1 by [TypeSafe AI's **Jev**](https://openrouter.ai/docs/guides/community/jev) — a "System One" non-autoregressive decision model reached via OpenRouter's alpha Decisions API, **not** AWS Bedrock — against a 0.75 threshold. Below threshold, the relevant step revises itself (bounded at 2 retries, then proceeds with its best attempt); an unreachable Jev degrades the gate to "passed" rather than blocking the pipeline on a third-party dependency.
- **Manual and AI-driven evaluation**, converging on one shared scoring computation and 7-band RAG (Red/Amber/Green) result.
- **Glassmorphism design system** (white + lemon yellow `#FFF700`), with an explicit glass-vs-solid rule: glass surfaces for chrome/cards/modals, solid panels for dense data (guideline matrices, KPI/weight tables).

## Architecture

### System overview

```mermaid
graph TD
    Browser["Browser"]

    subgraph Frontend["Frontend — Next.js 15 App Router"]
        UI["Chat / Charts / Evaluations / Settings"]
        ServerComp["Server Components & route handlers"]
    end

    subgraph Backend["Backend — FastAPI"]
        API["REST API — /api/v1"]
        Graph["LangGraph scorecard-builder state machine"]
        Judge["Ensemble LLM judge"]
        Formula["Scoring-formula engine (simpleeval)"]
    end

    subgraph DB["PostgreSQL 17"]
        Tables["Relational + JSONB tables<br/>scorecards · kpi_nodes · evaluations · chat_*"]
        PGVector["pgvector — scorecard_embeddings"]
        Ltree["ltree — kpi_nodes.path hierarchy"]
        Checkpoints["LangGraph checkpoint tables"]
    end

    subgraph AWSCloud["AWS"]
        Bedrock["AWS Bedrock Converse API<br/>Z.ai GLM-5 (chat) · GLM-4.7-Flash (judge)<br/>Titan Text Embeddings V2"]
        Gateway["AWS AgentCore Gateway<br/>web_search MCP tool, hand SigV4-signed"]
    end

    subgraph OpenRouterCloud["OpenRouter"]
        Jev["TypeSafe AI Jev — alpha Decisions API<br/>typesafe/jev-1.13, quality-gate scorer"]
    end

    Web(("Public web"))

    Browser -->|HTTPS| UI
    UI --> ServerComp
    ServerComp -->|NEXT_PUBLIC_API_BASE_URL / INTERNAL_API_BASE_URL| API
    API --> Graph
    API --> Judge
    Judge --> Formula
    Graph --> Formula
    API --> Tables
    Graph --> Checkpoints
    API --> PGVector
    API --> Ltree
    Graph -->|boto3 Converse| Bedrock
    Judge -->|boto3 Converse| Bedrock
    Graph -->|signed HTTPS POST, JSON-RPC/MCP| Gateway
    Gateway --> Web
    Graph -->|HTTPS POST /api/alpha/decisions, quality gates only| Jev
```

### Chat-to-scorecard flow (the research fan-out in detail)

```mermaid
sequenceDiagram
    actor User
    participant UI as Chat UI
    participant API as FastAPI /chat
    participant Graph as LangGraph orchestrator
    participant Sim as check_similarity
    participant Research as research_kpis (master)
    participant Agents as Research agents (N, parallel)
    participant Search as AgentCore Gateway web_search
    participant Jev as Jev (OpenRouter quality gate)
    participant Propose as propose_kpis (GLM-5)
    participant DB as Postgres

    User->>UI: Describe the desired scorecard
    UI->>API: POST /chat/sessions/{id}/messages
    API->>Graph: send_message (new turn or resume from interrupt)
    Graph->>Sim: check_similarity — runs once per session
    Sim->>DB: pgvector cosine search over scorecard_embeddings
    alt similar scorecard found, similarity >= 0.75
        Sim-->>Graph: suggest_similar triggers interrupt()
        Graph-->>API: pause, return suggestions
        API-->>UI: "Use as-is / Adapt / Start fresh"
        UI->>API: user's choice, resumes the graph
    end
    Graph->>Research: research_kpis — runs once per session
    Research->>Research: decide_research_angles (GLM-5 picks 0-4 angles)
    loop quality gate 1: plan vs. request, up to 2 revisions
        Research->>Jev: rate_match(instruction=user request, answer=plan)
        Jev-->>Research: 0-1 score
        Research->>DB: emit_turn_event (quality_gate / quality_gate_retry)
    end
    par one branch per research angle
        Research->>Agents: spawn a research agent via asyncio.gather
        Agents->>Search: web_search(query), up to 2 calls
        Search-->>Agents: real titles / URLs / snippets
        Agents->>Agents: record_research_finding
        loop quality gate 2: finding vs. assigned angle, up to 2 revisions
            Agents->>Jev: rate_match(instruction=angle+focus, answer=finding)
            Jev-->>Agents: 0-1 score
        end
        Agents->>Agents: propose_kpi_batch — 2-4 KPIs, each with full 11-level guidelines
        Agents-->>Research: ResearchFinding + this agent's proposed KPIs
    end
    Research->>Research: dedupe near-duplicates, cap at 24, renormalize weights to 100
    Research->>DB: emit_turn_event per actor (live trace UI)
    Research-->>Graph: merged KPI batch written into draft.kpis
    Graph->>Propose: propose_kpis — reconcile the researched draft (force_tool_use)
    loop quality gate 3: final answer vs. request, up to 2 revisions
        Propose->>Jev: rate_match(instruction=conversation, answer=decision)
        Jev-->>Propose: 0-1 score
        Propose->>DB: emit_turn_event (quality_gate / quality_gate_retry)
    end
    alt clarification needed
        Propose-->>Graph: ask_clarification triggers interrupt()
        Graph-->>API: pending question + chip options
        API-->>UI: show clarifying question
        UI->>API: user answers, resumes the graph
    else user confirms
        Propose->>Graph: update_draft(confirmed=true)
        Graph->>Graph: confirm node
        Graph->>DB: materialize_draft — writes Scorecard / ScorecardVersion / KpiNode / KpiGuideline
        Graph-->>API: saved scorecard reference
        API-->>UI: navigate to the Charts detail view
    end
```

### Evaluation flow (manual and AI ensemble, one shared computation)

```mermaid
sequenceDiagram
    actor Evaluator
    participant UI as Charts › Evaluate tab
    participant API as FastAPI /evaluations
    participant Judge as Ensemble judge (judge.py)
    participant Bedrock as AWS Bedrock — GLM-4.7-Flash
    participant Formula as compute_final_score()
    participant DB as Postgres

    Evaluator->>UI: Choose AI-assisted or manual scoring
    alt AI ensemble judge
        UI->>API: POST /evaluations/{id}/run with input_text
        API->>Judge: run_judge(evaluation, input_text)
        Judge->>Bedrock: one shared evidence-extraction call
        Note over Judge,Bedrock: per leaf KPI, run concurrently -- k=3 record_kpi_judgment calls, guideline order perturbed each time
        Bedrock-->>Judge: matched_level, evidence_quotes, reasoning, score (x3 per KPI)
        Judge->>Judge: aggregate by median, flag needs_review when spread exceeds 2 pts or levels disagree
    else Manual scoring
        Evaluator->>UI: Score every leaf KPI by hand
        UI->>API: POST /evaluations/{id}/results  (one score per KPI)
        UI->>API: POST /evaluations/{id}/finalize
    end
    API->>Formula: compute_final_score(leaves, scores, effective_weights, scoring_formula)
    Formula->>Formula: default weighted average, OR custom simpleeval formula, clamped to [0,10]
    Formula-->>API: final_weighted_score
    API->>API: rag_band_for_score() — 7-band RAG palette
    API->>DB: persist evaluation + evaluation_kpi_results
    API-->>UI: score, RAG band, per-KPI reasoning + verbatim evidence
```

## Tech stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI 0.115, Python 3.12, Uvicorn |
| ORM / migrations | SQLAlchemy 2.0 (async), Alembic, `psycopg[binary]` v3 |
| Validation | Pydantic v2 / pydantic-settings |
| Agent orchestration | LangGraph 1.2 + `langgraph-checkpoint-postgres` (Postgres-backed checkpointing) |
| LLM access | boto3 → AWS Bedrock Converse/ConverseStream API |
| Chat / builder model | Z.ai GLM-5 (`zai.glm-5`) |
| Judge model | Z.ai GLM-4.7-Flash (`zai.glm-4.7-flash`) — cheap enough to justify a k=3 ensemble |
| Embeddings | Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) |
| Web search | AWS Bedrock AgentCore Gateway (MCP `tools/call`, hand-rolled SigV4 over `httpx`) |
| Quality gate (self-critique) | TypeSafe AI **Jev** via OpenRouter's alpha Decisions API (`typesafe/jev-1.13`) — a separate provider from Bedrock, used only for the three 0–1 quality-rating checkpoints |
| Safe formula evaluation | `simpleeval` (AST-whitelisted, no `eval`/`exec`) |
| Database | PostgreSQL 17 (`pgvector/pgvector:pg17`), `pgvector` + `ltree` extensions |
| Frontend framework | Next.js 15 (App Router), React 19, TypeScript 5.7 |
| Styling / UI | Tailwind CSS 4, shadcn/ui on Radix primitives, `lucide-react` |
| Data viz / tables | Recharts, TanStack Table |
| Local dev / infra | Docker Compose (3 services: `db`, `backend`, `frontend`) |
| Lint / test (backend) | `ruff`, `pytest` + `pytest-asyncio` (against a real Postgres — no mocks) |
| Lint / test (frontend) | ESLint, `tsc --noEmit` (no dedicated JS test runner is wired up yet) |

## Project structure

```
ScoreSmith/
├── backend/                  FastAPI + SQLAlchemy + LangGraph + boto3
│   ├── app/
│   │   ├── ai/                Bedrock client, Jev/OpenRouter quality-gate client, scorecard-builder graph, judge, scoring-formula engine, web search
│   │   ├── api/v1/             REST routers: users, scorecards, kpi_nodes, evaluations, chat
│   │   ├── models/             SQLAlchemy ORM models (scorecards, kpi_nodes, evaluations, chat_*, ...)
│   │   ├── schemas/            Pydantic request/response schemas
│   │   └── scripts/            seed.py (idempotent Cycle 1 scenario catalogue), generate_scenarios.py
│   ├── alembic/                Database migrations
│   └── tests/                  pytest suite — 113 tests, run against a real Postgres instance
├── frontend/                  Next.js 15 App Router + TypeScript
│   ├── app/                    /chat, /charts, /evaluations, /settings routes ("/" redirects to /chat)
│   ├── components/             chat/, chart-detail/, charts-library/, evaluation-result/, design-system/, layout/
│   └── lib/                    API client, hooks, shared utils
├── infra/                     docker-compose.yml, .env.example, db-init scripts
├── docs/                      Initial product scope, data dictionary, Cycle 2/3 plan, cloud deployment plan
└── References/                Source framework PDFs (RAG Quality Scorecard, Data-Driven Development)
```

## Getting started / local development

Requires Docker + Docker Compose, and AWS Bedrock access (a Bedrock "API key" bearer-token credential or a SigV4 IAM key pair) for the AI features — the rest of the app (scorecard CRUD, manual evaluation) works without Bedrock configured.

```bash
cd infra
cp .env.example .env
# Fill in at minimum: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (see the note below), AWS_REGION.
# Optionally fill in AGENTCORE_GATEWAY_* to enable real web search in the research fan-out —
# left blank, web_search.py just returns no results rather than failing the chat turn.
# Optionally fill in OPENROUTER_JEV_API to enable the Jev quality-gate self-critique layer —
# left blank, every gate check degrades to "passed" rather than blocking the chat turn.
docker compose up --build
```

- Frontend: <http://localhost:3000>
- Backend API: <http://localhost:8000> (interactive docs at `/docs`, health check at `/health`)
- Postgres: `localhost:5432`

Migrations and seed data are **not** run automatically on container start — run them once after the stack is up:

```bash
docker compose exec backend python -m alembic upgrade head
docker compose exec backend python -m app.scripts.seed
```

### Key environment variables (`infra/.env.example`)

| Variable | Purpose |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_PORT` | Local Postgres container |
| `TEST_DATABASE_URL` | Sibling test database, kept separate so `pytest`'s per-test `TRUNCATE` never touches seeded dev data |
| `BACKEND_PORT` / `FRONTEND_PORT` | Compose host port mappings |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Bedrock credentials. **Note**: if your credential is a Bedrock long-term "API key" (bearer token) rather than a real SigV4 IAM pair, plain `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` fails with `UnrecognizedClientException`; `docker-compose.yml` auto-derives `AWS_BEARER_TOKEN_BEDROCK` from `AWS_SECRET_ACCESS_KEY` (prefixed `A`) to work around this — see the comments in `infra/docker-compose.yml` and `infra/.env.example` for the full story |
| `BEDROCK_CHAT_MODEL_ID` / `BEDROCK_JUDGE_MODEL_ID` / `BEDROCK_EMBEDDING_MODEL_ID` | Default to `zai.glm-5`, `zai.glm-4.7-flash`, `amazon.titan-embed-text-v2:0` |
| `AGENTCORE_GATEWAY_WEB_SEARCH_URL` / `_TOOL_NAME` / `_AWS_ACCESS_KEY_ID` / `_AWS_SECRET_ACCESS_KEY` | AWS AgentCore Gateway web-search tool — a separate credential pair from the main Bedrock ones; the only web-search backend this project talks to |
| `OPENROUTER_JEV_API` | OpenRouter API key for the Jev quality-gate layer (`app/ai/jev_client.py`) — a separate provider from Bedrock. Left blank, every gate check degrades to "passed" rather than blocking the chat turn |
| `NEXT_PUBLIC_API_BASE_URL` | Backend URL as seen by the *browser* (client-side fetches) |
| `INTERNAL_API_BASE_URL` | Backend URL as seen *inside* the frontend container (server components / route handlers), via the Compose service name |
| `JWT_SECRET` | Present in `.env.example` for future real auth; not currently read anywhere in the backend — see [Current limitations](#current-limitations) |

## Running tests

### Backend

Tests run against a **real** Postgres instance (no mocks for the DB layer) — a sibling `quality_scorecard_test` database, auto-derived by `tests/conftest.py` from `DATABASE_URL` unless `TEST_DATABASE_URL` is set explicitly.

```bash
cd backend
uv venv --python 3.12 .venv && uv pip install --python .venv -e ".[dev]"   # or: python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
# one-time: create the test DB with the vector + ltree extensions (already done by
# infra/db-init/02-create-test-db.sql on a fresh docker-compose volume)
.venv/bin/python -m pytest        # 113 tests as of this writing
.venv/bin/python -m ruff check .  # lint
```

(Windows note: `psycopg`'s async mode needs the selector event loop, which `app/main.py` and `tests/conftest.py` already set automatically on `sys.platform == "win32"`.)

### Frontend

There is no dedicated JavaScript test runner wired up yet — the quality gates are the TypeScript compiler, ESLint, and a production build:

```bash
cd frontend
npx tsc --noEmit
npm run lint
npm run build
```

## Status & roadmap

This build corresponds to **Phase 0 + Cycle 1** ("Data Foundation") of the Data-Driven Development Framework (`References/Data_Driven_Development_Framework_v1.1.pdf`): schema + data dictionary (`docs/data_dictionary.md`), an idempotent scenario catalogue (`backend/app/scripts/generate_scenarios.py`), and a working first build — the chat scorecard builder, the ensemble LLM judge, and the Charts/Evaluations UI.

**Explicitly deferred**, and scoped concretely in `docs/cycle_2_3_plan.md`:
- **Cycle 2 (Test & Migration)** — deliberately-flawed/migration data hardening: concurrent-edit races on KPI weights, partial-failure materialization, embedding-model drift at volume, real messy spreadsheet import, referential races on delete-during-evaluation.
- **Cycle 3 (Business Behaviour, Specification & Testing)** — the scorecard `draft → published → archived` lifecycle is defined in the schema but not enforced by any endpoint; the framework's QTC (Quality/Time/Cost) gate is not modeled (only Quality is computed today); Red-diagnosis and Aptitude/Capability/Competency tracking are not implemented at all; a `needs_review`-flagged KPI result has no attached human review workflow yet.

See `docs/cloud_deployment_plan.md` for the (also planning-only) path from today's local Docker Compose stack to a real AWS deployment (Aurora PostgreSQL, Secrets Manager, App Runner, CI/CD).

## Current limitations

Documented honestly rather than glossed over:

- **Auth is a dev-only stub.** `app/deps.py::get_current_user` trusts a raw `X-User-Id` header or an unsigned `Authorization: Bearer <user-id>` token — there is no signature verification, no token expiry, and no password/identity check of any kind. Real OIDC/OAuth2 auth is explicitly deferred.
- **No RBAC / multi-tenancy yet.** `users.org_id` and `users.role` exist as plain scalar columns with no enforcement; a `role` string is a documented convention, not a DB-enforced permission.
- **The weight-sum-to-100 rule is DB-enforced but only at commit time** (a deferred trigger), which is why sibling KPI groups must be created via the bulk endpoints rather than one node at a time — see `backend/app/api/v1/kpi_nodes.py`'s module docstring.
- **No automated regression coverage for the Cycle 2 failure scenarios yet** (concurrent edits, partial materialization failure, etc.) — they're scoped, not built.
- **The frontend has no dedicated test framework** — `tsc`, ESLint, and a production build are the current gates.
