# Architecture

Score Smith (the sidebar and a few backend identifiers still say "Quality Scorecards" / `qs_*`) is a Next.js front end, a stateless FastAPI
API, one or more background workers, Postgres (the system of record and the job queue) and an optional Redis. This page
has the diagrams and short narratives; the details live next to the code:
[backend/README.md](../backend/README.md), [frontend/README.md](../frontend/README.md), [infra/README.md](../infra/README.md),
[configuration.md](configuration.md), [api-reference.md](api-reference.md), [plan-sharing-rbac.md](plan-sharing-rbac.md),
[ai-features.md](ai-features.md).

Contents: [1 System](#1-system-architecture) | [2 Evaluation lifecycle](#2-evaluation-pipeline-and-lease-lifecycle) |
[3 Chat turn](#3-chat-turn-lifecycle) | [4 Auth](#4-auth-and-refresh-flow) | [5 Sharing](#5-sharing-and-permissions) |
[6 Data model](#6-data-model) | [7 Frontend routes](#7-frontend-route-and-redirect-map) | [8 Narratives](#8-how-things-flow)

## 1. System architecture

```mermaid
flowchart LR
    Browser["Browser"]
    subgraph FE["frontend container (Next.js 15)"]
        MW["middleware.ts<br/>route gate + silent refresh"]
        Pages["App Router pages<br/>(marketing) (auth) (app)"]
        Proxy["rewrite /api/v1/* to INTERNAL_API_BASE_URL"]
    end
    subgraph API["API replicas (ROLE=api, gunicorn + uvicorn)"]
        A1["FastAPI /api/v1<br/>/health /ready"]
    end
    subgraph WK["worker replicas (ROLE=worker, python -m app.worker)"]
        W1["evaluation dispatcher<br/>chat turn runner<br/>/health /ready /metrics"]
    end
    PG[("Postgres 17<br/>pgvector + ltree<br/>data, queues, leases")]
    RD[("Redis<br/>shared LLM limits<br/>rate limits (optional)")]
    subgraph AWS["AWS"]
        S3[("S3")]
        SFN["Step Functions<br/>+ 6 Lambdas"]
        BR["Bedrock<br/>GLM-5, GLM-4.7-Flash, Maverick, Titan"]
        GW["AgentCore Gateway<br/>web search"]
        CW["CloudWatch<br/>BacklogPerWorker"]
    end
    OR["OpenRouter<br/>Jev decisions API"]

    Browser -->|"HTTPS, cookies"| MW
    MW --> Pages
    Browser -->|"/api/v1/*"| Proxy
    Proxy --> A1
    Browser -.->|"presigned multipart PUT"| S3
    A1 --> PG
    A1 -.-> RD
    W1 --> PG
    W1 -.-> RD
    W1 -->|"StartExecution, poll"| SFN
    SFN <--> S3
    SFN --> BR
    W1 --> S3
    W1 --> BR
    W1 --> OR
    W1 --> GW
    W1 -.->|"if AUTOSCALE_METRIC_NAMESPACE"| CW
    A1 --> BR
```

Key points

- The browser only ever talks to the Next.js origin. `/api/v1/*` is proxied to the API, so session cookies are first-party and
  the backend URL never reaches client bundles. The API port (8000) is bound to loopback.
- API replicas are stateless and never run long jobs: they validate, write rows and answer. Workers claim queued rows
  with `SELECT ... FOR UPDATE SKIP LOCKED`, hold a *lease* on them and heartbeat it.
- Postgres is both the database and the job queue (no broker). Redis is optional and only shares *limits*
  (cluster-wide Bedrock/Jev/web-search concurrency, rate-limit counters); every Redis path **fails open** to a per-process limit.
- The whole stack runs in one process with `ROLE=all` (the default for local `uvicorn` and the test suite).

```
  browser --> [Next.js :3000] --/api/v1/*--> [API xN :8000] --> [Postgres] <-- [worker xN] --> Step Functions/Lambda/S3
                  |  middleware: auth gate                 \--> [Redis]  <---------/      \--> Bedrock, OpenRouter (Jev), AgentCore
                  \-- presigned S3 upload (browser -> S3 directly)
```

## 2. Evaluation pipeline and lease lifecycle

An evaluation row is the unit of work. `cancelled` is not a status: it is `failed` with `error_code = cancelled`.
`processing` exists in the enum but is unused.

```mermaid
stateDiagram-v2
    [*] --> queued: POST /evaluations/ai/jobs (user slot free)
    queued --> ingesting: worker claims row, sets lease, starts Step Functions
    ingesting --> scoring: execution SUCCEEDED
    scoring --> completed: score, RAG band saved
    scoring --> scoring: transient failure, retry (AI_EVAL_SCORING_RETRIES, 20s then 60s)
    ingesting --> failed: execution failed or timeout
    scoring --> failed: scoring_failed or timeout
    queued --> failed: cancel
    ingesting --> failed: cancel flag seen (error_code cancelled)
    scoring --> failed: cancel flag seen (error_code cancelled)
    failed --> queued: POST /retry (new attempt)
    ingesting --> ingesting: lease expired, another worker adopts and resumes by execution ARN
    scoring --> scoring: lease expired, another worker adopts (scoring is idempotent)
    completed --> [*]
```

```mermaid
sequenceDiagram
    participant API as API replica
    participant PG as Postgres
    participant W1 as Worker A
    participant W2 as Worker B
    participant SF as Step Functions
    API->>PG: lock user slot, insert queued rows (Idempotency-Key checked first)
    API-->>API: 202 Accepted
    W1->>PG: claim oldest queued (per-user round robin) if fewer than N active
    W1->>PG: lease_owner, lease_expires_at = now + LEASE_SECONDS
    W1->>SF: StartExecution (name = evaluation id, -aN on retry)
    loop every LEASE_HEARTBEAT_SECONDS
        W1->>PG: renew lease, read cancel_requested_at
    end
    Note over W1,PG: Worker A dies, lease stops being renewed
    W2->>PG: lease expired, adopt row
    W2->>SF: DescribeExecution by sfn_execution_arn, keep polling
    SF-->>W2: SUCCEEDED
    W2->>PG: status scoring, run LangGraph scoring, save results, notify
```

| Mechanism | Detail |
|---|---|
| Claim | `AI_EVAL_MAX_CONCURRENT` (clamped 1-5) active evaluations cluster-wide, enforced under a Postgres advisory lock. Queued rows are ordered by (per-user rank, `queued_at`) so one user's batch cannot starve others. |
| Lease | `lease_owner`, `lease_expires_at`, `heartbeat_at` on `evaluations`. `LEASE_SECONDS=60`, heartbeat every `LEASE_HEARTBEAT_SECONDS=15`. Only an *expired* (or released) lease may be adopted, so a live owner's spend is never doubled. |
| Drain | On SIGTERM a worker stops claiming and releases its leases so another worker resumes at once. |
| Cancel | `cancel_requested_at` is a DB flag that whichever worker drives the row sees (polling loop, between KPIs). The API need not own the driver. A chart moved to the trash cancels its jobs with `cancel_reason = cancelled_by_trash`; restoring it re-queues exactly those rows when the runner's slot is free. |
| Retries | Scoring retries after transient model failures (`AI_EVAL_SCORING_RETRIES`, delays `AI_EVAL_SCORING_RETRY_DELAYS`). Manual retry only from `failed`, new `attempt`, fresh execution name. |
| Slot | At most one running evaluation job per user (a batch is one job). Derived from row state, so it cannot leak. `409 user_job_active`. |
| Timeout | `AI_EVAL_TIMEOUT_SECONDS` (default 14400) fails the row with `timeout`. |
| Idempotency | `Idempotency-Key` on job creation: a replay returns the stored result before the slot check. |

## 3. Chat turn lifecycle

```mermaid
stateDiagram-v2
    [*] --> idle
    idle --> queued: POST message returns 202 (slot free, no turn running on the session)
    queued --> running: worker claims turn, takes lease
    running --> running: heartbeat renews lease, supervisor polls cancel flag
    running --> idle: persisted result (draft, question or saved chart) and notification
    running --> failed_cancelled: cancel requested (POST cancel)
    running --> failed_timeout: CHAT_TURN_TIMEOUT_SECONDS exceeded
    running --> failed_bedrock: bedrock_unavailable
    running --> failed_other: turn_failed
    running --> failed_interrupted: worker drained or lease expired and reaped
    failed_cancelled --> idle: user resends
    failed_timeout --> idle: user resends
    failed_bedrock --> idle: user resends
    failed_other --> idle: user resends
    failed_interrupted --> idle: user resends
```

- `turn_error_code` values: `bedrock_unavailable`, `timeout`, `interrupted`, `cancelled`, `turn_failed`.
- Only one running turn per user across all sessions: `409 user_chat_active`. A second message on the same session while a turn runs: `409`
  with `turn_in_progress`.
- The browser polls `GET /chat/sessions/{id}` (interim draft while running) and `/turn-events` (live trace); state is durable in Postgres,
  so a refresh or a second tab resumes. The graph state is checkpointed by LangGraph in Postgres.
- Shared recipients read the conversation and KPIs (`/chat/shared/{id}`) but cannot post.

## 4. Auth and refresh flow

Cookies: `qs_access` (httpOnly, 15 min JWT with `rol` claim), `qs_refresh` (httpOnly, opaque, rotating, session cookie unless "remember me" = 30 days),
`qs_csrf` (readable, double-submit). All `Secure` in production, `SameSite=Lax`.

```mermaid
sequenceDiagram
    participant B as Browser
    participant M as Next middleware
    participant A as API
    B->>A: POST /auth/login (via proxy)
    A-->>B: Set-Cookie qs_access, qs_refresh, qs_csrf
    B->>M: GET /charts
    alt access token valid
        M-->>B: render page
    else access expired or missing, refresh present
        M->>A: POST /auth/refresh (cookies + X-CSRF-Token + Origin)
        A->>A: rotate refresh token, new family member
        A-->>M: 200 + Set-Cookie
        M-->>B: render page with renewed cookies
    else refresh rejected
        M-->>B: redirect /login?next=/charts&expired=1, cookies cleared
    end
    Note over B,A: Browser API call returns 401: api-client refreshes once (single flight), retries, else goes to /?expired=1
    Note over A: Reused rotated refresh token (outside 10 s two-tab grace) revokes the whole family
```

```mermaid
flowchart TD
    Req["Request path"] --> Sgn{"/ or /login, /signup, /setup,<br/>/forgot-password, /reset-password ?"}
    Sgn -->|yes| Exp{"?expired=1 ?"}
    Exp -->|yes| Clear["clear cookies, show page"]
    Exp -->|no| Valid{"access token valid,<br/>or renewable by refresh cookie?"}
    Valid -->|yes| Go["redirect to /chat<br/>(or safe ?next= on /login)"]
    Valid -->|no| Show["show page"]
    Sgn -->|no| Pub{"/verify-email ?"}
    Pub -->|yes| Next["show page"]
    Pub -->|no| Auth{"valid access token<br/>or successful renewal?"}
    Auth -->|yes| Role{"/admin or /settings<br/>and role is not admin?"}
    Role -->|yes| Chat["redirect to /chat"]
    Role -->|no| Ok["render"]
    Auth -->|"refresh rejected"| Login["redirect /login?next=...<br/>cookies cleared"]
    Auth -->|"backend unreachable"| Keep["keep cookies, page shows its own error"]
```

Other rules: lockout after `LOCKOUT_THRESHOLD` (5) bad passwords, doubling from 5 min up to 60 min; `429` on per-IP and per-address
login limits (`RATE_LIMIT_LOGIN`, default 10/min); `POST /auth/logout` revokes the presented session and ends on `/`; admin password change,
deactivation and role change revoke every session (`users.sessions_valid_after`).

## 5. Sharing and permissions

Chart invitation (`scorecard_invitations.status`) and what happens to the collaborator row:

```mermaid
stateDiagram-v2
    [*] --> pending: invite by username or email (owner or any editor)
    pending --> pending: same invite again (idempotent, already_pending)
    pending --> accepted: invitee accepts (after optional preview)
    pending --> declined: invitee declines
    pending --> revoked: inviter or owner revokes
    accepted --> revoked: collaborator removed by owner, or leaves
    declined --> [*]
    revoked --> [*]
    note right of accepted
        collaborator row exists: role editor,
        can edit, run evaluations, re-share
    end note
```

A declined or revoked invitation can be sent again (the unique index only covers `pending`). A pending invitee has no access except
`GET /invitations/{id}/preview`. Chat shares are separate rows (`chat_shares`): read-only, optionally with a chart invitation (`with_chart`);
recipients can pass a chat on (still read-only) and save their own copy of the draft once.

Permission matrix (404 means "does not exist" for anyone without access; admins have **no** data bypass):

| Action | Owner | Editor (accepted) | Pending invitee | Declined / revoked / removed | Other user | Admin (not shared) |
|---|---|---|---|---|---|---|
| Read chart, versions, KPIs, evaluations, activity | yes | yes | preview only | 404 | 404 | 404 |
| Edit chart, KPIs, weights, guidelines; run / cancel / retry / export evaluations | yes | yes | 404 | 404 | 404 | 404 |
| Invite further people (re-share) | yes | yes | 404 | 404 | 404 | 404 |
| Revoke invitations / remove collaborators | any | only invitations they sent, else 403 `owner_only` | 404 | 404 | 404 | 404 |
| Delete chart | moves to trash | leaves the chart (204) | 404 | 404 | 404 | 404 |
| Trash: list, restore, purge | yes | no | no | no | no | no |
| Chat sessions | private to its user (always, admins too); read-only via chat share | | | | | |
| `/admin/users*`, Users and Settings pages | admin only (403 for users) | | | | | |

Concurrent edits: `PATCH` accepts `If-Match: <updated_at>`; a mismatch is `409 stale_edit` with the current `updated_at`.

## 6. Data model

Main tables only (all primary keys are UUIDs except `scorecard_activity.id`; "Q" = columns that make the row a queue item / lease).

```mermaid
erDiagram
    USERS ||--o{ SCORECARDS : owns
    USERS ||--o{ SCORECARD_COLLABORATORS : "is editor via"
    USERS ||--o{ SCORECARD_INVITATIONS : "sends and receives"
    USERS ||--o{ CHAT_SESSIONS : owns
    USERS ||--o{ CHAT_SHARES : "receives"
    USERS ||--o{ NOTIFICATIONS : receives
    USERS ||--o{ REFRESH_TOKENS : has
    USERS ||--o{ EVALUATIONS : "runs (owner_id)"
    USERS ||--o{ AUDIT_LOG : "acts in"
    SCORECARDS ||--o{ SCORECARD_VERSIONS : has
    SCORECARDS ||--o{ SCORECARD_COLLABORATORS : shared_with
    SCORECARDS ||--o{ SCORECARD_INVITATIONS : invites
    SCORECARDS ||--o{ SCORECARD_ACTIVITY : logs
    SCORECARDS ||--o{ EVALUATION_BATCHES : groups
    SCORECARD_VERSIONS ||--o{ KPI_NODES : contains
    SCORECARD_VERSIONS ||--o| SCORECARD_EMBEDDINGS : embedded_as
    SCORECARD_VERSIONS ||--o{ EVALUATIONS : scored_by
    KPI_NODES ||--o{ KPI_NODES : parent_of
    KPI_NODES ||--o{ KPI_GUIDELINES : "11 levels"
    EVALUATION_BATCHES ||--o{ EVALUATIONS : contains
    EVALUATIONS ||--o{ EVALUATION_KPI_RESULTS : produces
    EVALUATIONS ||--o{ EVALUATION_SOURCES : reads
    EVALUATIONS ||--o{ EVALUATION_EVENTS : traces
    KPI_NODES ||--o{ EVALUATION_KPI_RESULTS : scored_in
    CHAT_SESSIONS ||--o{ CHAT_MESSAGES : has
    CHAT_SESSIONS ||--o{ CHAT_TURN_EVENTS : traces
    CHAT_SESSIONS ||--o{ CHAT_SHARES : shared_as

    USERS {
        uuid id PK
        string email UK
        string username "unique, case-insensitive"
        string role "admin or user"
        bool is_active
        datetime deleted_at "anonymised tombstone"
        datetime sessions_valid_after
    }
    SCORECARDS {
        uuid id PK
        uuid owner_id FK
        uuid current_version_id FK
        datetime deleted_at "trash"
    }
    SCORECARD_VERSIONS {
        uuid id PK
        uuid scorecard_id FK
        int version_number
        string scoring_formula
    }
    KPI_NODES {
        uuid id PK
        uuid scorecard_version_id FK
        uuid parent_id FK
        ltree path
        decimal weight "leaves only"
    }
    SCORECARD_COLLABORATORS {
        uuid scorecard_id FK
        uuid user_id FK
        string role "editor"
        uuid invited_by FK
    }
    SCORECARD_INVITATIONS {
        uuid id PK
        uuid scorecard_id FK
        uuid inviter_id FK
        uuid invitee_id FK
        string status "pending accepted declined revoked"
    }
    SCORECARD_ACTIVITY {
        bigint id PK
        uuid scorecard_id FK
        uuid actor_id FK
        string action "append-only"
    }
    EVALUATIONS {
        uuid id PK
        uuid scorecard_version_id FK
        uuid owner_id FK
        uuid batch_id FK
        string status "Q"
        string lease_owner "Q"
        datetime lease_expires_at "Q"
        datetime cancel_requested_at "Q"
        string cancel_reason
    }
    EVALUATION_KPI_RESULTS {
        uuid id PK
        uuid evaluation_id FK
        uuid kpi_node_id FK
        decimal score
        bool needs_review
    }
    EVALUATION_BATCHES {
        uuid id PK
        uuid scorecard_id FK
        uuid created_by FK
    }
    CHAT_SESSIONS {
        uuid id PK
        uuid user_id FK
        uuid target_scorecard_id FK
        datetime pending_turn_started_at "Q"
        string turn_lease_owner "Q"
        datetime turn_lease_expires_at "Q"
    }
    CHAT_SHARES {
        uuid id PK
        uuid session_id FK
        uuid recipient_id FK
        uuid shared_by FK
        bool with_chart
        uuid saved_scorecard_id FK
    }
    NOTIFICATIONS {
        uuid id PK
        uuid user_id FK
        string type
        string dedupe_key "unique per user"
        datetime read_at
    }
    REFRESH_TOKENS {
        uuid id PK
        uuid user_id FK
        uuid family_id
        string token_hash UK
        datetime used_at
    }
    IDEMPOTENCY_KEYS {
        uuid user_id PK
        string scope PK
        string key PK
        string request_hash
    }
    AUDIT_LOG {
        uuid id PK
        uuid actor_id FK "SET NULL on user purge"
        string entity_type
        string action
    }
```

Not drawn: `kpi_guidelines` columns, `password_reset_tokens`, `oauth_identities` (all `user_id` FKs), `evaluation_sources`, `evaluation_events`,
`chat_messages`, `chat_turn_events`, `scorecard_embeddings`, LangGraph checkpoint tables (created by `python -m app.scripts.migrate`).
`idempotency_keys.user_id` has no foreign key. Column-level detail: [data_dictionary.md](data_dictionary.md).

Migrations: `0001`..`0010` foundation, chat, AI evaluation; `0011_scaling_leases` (leases, cancel flags, chat-turn queue, `idempotency_keys`);
`0012_auth_ownership` (passwords, tokens, `evaluations.owner_id`, audit context); `0013_sharing_rbac` (usernames, collaborators, invitations, activity,
notifications, two roles, list indexes); `0014_chat_shares`; `0015_chart_trash` (`scorecards.deleted_at/deleted_by`); `0016_eval_cancel_reason`.

## 7. Frontend route and redirect map

```mermaid
flowchart LR
    subgraph Marketing["(marketing)"]
        Home["/ homepage"]
    end
    subgraph Auth["(auth)"]
        Login["/login"]
        Signup["/signup"]
        Setup["/setup"]
        Forgot["/forgot-password"]
        Reset["/reset-password"]
        Verify["/verify-email"]
    end
    subgraph App["(app) requires a session"]
        Chat["/chat, /chat/[sessionId], /chat/shared/[sessionId]"]
        Charts["/charts, /charts/[id], /charts/trash"]
        EvalRes["/charts/[id]/evaluations/[evaluationId]"]
        Evals["/evaluations"]
        Settings["/settings (admin)"]
        Users["/admin/users (admin)"]
    end
    Home -->|"signed in"| Chat
    Login -->|"signed in or after login (safe ?next=)"| Chat
    App -->|"no session"| Login
    App -->|"session died"| Home
    Settings -->|"role user"| Chat
    Users -->|"role user"| Chat
    Chat -->|"sign out"| Home
```

## 8. How things flow

**A request.** The browser calls `/api/v1/...` on the Next.js origin with cookies. For unsafe methods the client adds `X-CSRF-Token` (copy of
`qs_csrf`). Next rewrites it to an API replica. `get_current_user` verifies the access JWT (cookie or `Authorization: Bearer`), checks the
CSRF header and the `Origin`, loads the user, and endpoints load every resource through owner/collaborator-scoped helpers in `app/authz.py`
(404 on miss). Rate limits are dependencies (per IP for auth routes, per user for expensive ones). Responses carry `X-Request-ID`, which is also
stored on audit rows.

**An evaluation.** (1) The browser uploads files straight to S3 with presigned multipart URLs (or sends Drive links / a spreadsheet to be parsed).
(2) `POST /evaluations/ai/jobs` takes the user's slot under an advisory lock, inserts `queued` rows and returns 202. (3) A worker claims a row,
leases it and starts a Step Functions execution; Lambdas download, extract, transcribe and OCR into `derived/{id}/corpus.json`. (4) On success
the same worker scores locally (LangGraph: identify, evidence, Jev score, reasoning), writes `evaluation_kpi_results`, the weighted score and RAG
band, and creates notifications. (5) The UI polls `GET /evaluations/{id}/progress`; the list uses `GET /evaluations/page` + `POST /evaluations/refresh`.
If the worker dies, another adopts the row after the lease expires. Details: section 2 and [ai-features.md](ai-features.md).

**A chat turn.** `POST /chat/sessions/{id}/messages` claims the user's chat slot and the session marker in one transaction, stores the message
and returns 202. A worker claims the turn, runs the LangGraph builder (mode detection, category research with web search, Jev quality gates,
guideline writing), streams trace events to `chat_turn_events`, and persists the draft or question. Confirming the draft materialises scorecard,
version, KPI nodes and guidelines. The UI polls the session and its trace. Details: section 3.
