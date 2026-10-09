"""Application configuration, sourced from environment variables (.env in local dev)."""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Substrings that mark a value as a placeholder / known-weak default (never acceptable in production).
_WEAK_MARKERS = ("change_me", "changeme", "dev-only", "example", "placeholder", "password")


def _weak(value: str) -> bool:
    low = value.lower()
    return any(m in low for m in _WEAK_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # REQUIRED (no default, so no password lives in the code). Must be a psycopg (v3) URL:
    # postgresql+psycopg://user:pass@host:port/db  - set it in the environment / .env (see infra/.env.example).
    database_url: str = ""

    # Comma-separated list of allowed CORS origins for the frontend. Production must set it explicitly.
    cors_origins: str = "http://localhost:3000"

    # ENV=production (alias: ENVIRONMENT) switches on the strict startup checks. Anything else is non-production.
    environment: str = Field(default="development", validation_alias=AliasChoices("ENV", "ENVIRONMENT"))

    # --- Process role / horizontal scaling ---
    # api    : serves HTTP only (run under gunicorn + uvicorn workers). Long jobs (evaluation drivers,
    #          background chat turns) are only ENQUEUED in Postgres; a `worker` process picks them up.
    # worker : `python -m app.worker` - the evaluation dispatcher + background chat turns + a small
    #          /health server. No public HTTP API.
    # all    : both in one process (local dev, the default, and the test suite).
    role: str = "all"
    # Lease protocol shared by evaluations and chat turns: a claim sets owner + lease, the owner renews
    # it every `lease_heartbeat_seconds`, and only a row whose lease has EXPIRED may be adopted by another
    # process (the previous owner died or was partitioned). The lease must outlive a few missed beats.
    lease_seconds: float = 60.0
    lease_heartbeat_seconds: float = 15.0
    # How often a worker checks its running chat turns for a cancel request / deleted session.
    chat_supervisor_seconds: float = 2.0
    # Chat turns one worker runs at once (each is a long LangGraph run).
    chat_turn_max_concurrent: int = 8
    # Port of the worker's small health server (`python -m app.worker`).
    worker_health_port: int = 8001
    # On SIGTERM a worker waits this long for running chat turns to finish before cancelling them.
    worker_drain_grace_seconds: float = 20.0
    # "text" (default, local dev) or "json" (one JSON object per line, with evaluation_id / session_id).
    log_format: str = "text"

    # --- Database pool (per process: size it as pool_size + max_overflow <= Postgres max_connections / processes) ---
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    db_pool_timeout_seconds: int = 30

    # --- Redis: cluster-wide LLM concurrency (app/limits/redis_semaphore.py) ---
    # Empty = disabled: every limit is per process only. When set but unreachable, the limiters FAIL OPEN
    # to the per-process limits (never blocks work because Redis is down).
    redis_url: str = ""
    # Cluster-wide caps; 0 = use the matching per-process setting (bedrock_max_concurrency / the web-search
    # client's own cap). Jev is not capped cluster-wide unless set.
    bedrock_global_concurrency: int = 0
    jev_global_concurrency: int = 0
    web_search_global_concurrency: int = 0
    # A slot a crashed process never released frees itself after this long.
    redis_slot_lease_seconds: int = 600
    # After a Redis error, skip Redis for this long before probing it again.
    redis_retry_after_seconds: float = 10.0

    # --- Autoscaling signal: backlog per worker = (queued + active evaluations + waiting chat turns) / workers ---
    # Non-empty: also published to CloudWatch as `BacklogPerWorker` under this namespace (always logged).
    autoscale_metric_namespace: str = ""
    autoscale_metric_interval_seconds: float = 60.0

    # --- AWS Bedrock (Cycle 1c AI core) ---
    # See infra/.env.example. Model is Z.ai GLM-5 (GLM-5 has no Bedrock embedding
    # endpoint, so embeddings stay on Titan Text Embeddings V2). Credentials use boto3's
    # normal resolution chain (env vars / shared profile / SSO / bearer token — see
    # infra/docker-compose.yml's AWS_BEARER_TOKEN_BEDROCK for this environment's real
    # credential shape) — not read individually here, since boto3.Session() picks them
    # up itself; only the region and model ids are app config.
    #
    # Model id: plain "zai.glm-5" (NOT "global.zai.glm-5") — confirmed live against this
    # account's Bedrock access on 2026-09-29: `global.zai.glm-5` returns "ValidationException:
    # The provided model identifier is invalid" (no matching cross-region inference
    # profile exists for this model in this account), while `list_foundation_models()`
    # lists plain `zai.glm-5` as directly invocable, and a live Converse call against it
    # succeeds.
    #
    # The JUDGE model is a separate, cheaper/faster id: "zai.glm-4.7-flash" (Z.ai's GLM
    # 4.7 Flash) — also confirmed live on Bedrock, directly invocable with no region
    # prefix needed, same Converse + tool-calling pattern as zai.glm-5. Judge-only: the
    # chat/builder model (bedrock_chat_model_id) stays on zai.glm-5. Flash's low cost is
    # what justifies running the judge as a k=3-call ensemble per KPI (see app/ai/judge.py)
    # instead of a single call.
    aws_region: str = "us-east-1"
    bedrock_chat_model_id: str = "zai.glm-5"
    bedrock_judge_model_id: str = "zai.glm-4.7-flash"
    bedrock_embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    # Output-token ceiling sent as Converse `inferenceConfig.maxTokens` on every call. The
    # code used to send none, leaving a large single tool call (a 20+ KPI `update_draft`, or
    # a batch of KPIs with 11-level rubrics each) at the mercy of the model's default output
    # limit and silently truncated. 0 = omit the field (provider default). Verify against the
    # model's documented max output before raising it.
    bedrock_max_output_tokens: int = 16000
    # Max Bedrock calls in flight at once from the research/enrichment fan-out (a process-wide
    # semaphore, see scorecard_builder._run_bedrock) — bounds throttling when a 40+ KPI
    # request fans out many agents and rubric chunks.
    bedrock_max_concurrency: int = 8
    # botocore transport settings for the bedrock-runtime client. botocore's DEFAULT read timeout
    # is 60s, which a large structured output (several KPIs x 11 rubric levels on GLM-5) exceeds,
    # and its default pool is 10 connections, which the concurrent fan-out overflows
    # ("Connection pool is full"). The pool is sized to max(this, 2 x concurrency + 8).
    bedrock_read_timeout_seconds: int = 240
    bedrock_connect_timeout_seconds: int = 10
    # Total attempts (1 = no botocore retry). botocore also retries read timeouts, and the app
    # already splits-and-retries timed-out guideline calls itself, so keep this small.
    bedrock_max_attempts: int = 2
    bedrock_max_pool_connections: int = 32

    # --- User-specified / hybrid KPI preparation (guideline fill + weighting) ---
    # KPIs per guideline-writing call: small so each call (11 rubric levels per KPI) finishes
    # well inside the read timeout.
    user_kpis_per_fill_call: int = 5
    # Wall-clock budget (seconds) for the bounded web-research stage over the user's KPIs
    # (<= 3 agents, one search each); on expiry the guidelines are written from model knowledge.
    user_research_timeout_seconds: int = 45
    # Wall-clock budget for the whole enrichment phase; after it, remaining KPIs get
    # best-effort model-knowledge guidelines in the smallest calls (see _enrich_user_kpis).
    user_enrich_deadline_seconds: int = 170

    # --- Open-ended pipeline: guideline writing + quality-gate cost control ---
    # Wall-clock budget (seconds) for ONE category's guideline-writing fan-out (compact rubric
    # chunks of `user_kpis_per_fill_call` KPIs); once passed, remaining KPIs use the smallest
    # calls without research context and, as a last resort, the marked fallback rubric.
    open_fill_deadline_seconds: int = 150
    # Quality gate (Jev) cost control. Per-call hard timeout (a slow Jev call counts as
    # "unreachable" = gate passed), and a per-turn budget: once a turn has been running longer
    # than `quality_gate_budget_seconds`, no further gate rating / critique / revision starts.
    quality_gate_call_timeout_seconds: float = 12.0
    quality_gate_budget_seconds: int = 150

    # Cheap intent router in front of the main-model request classification
    # (app/ai/request_routing.py): auto = Jev `choice` then the small judge model;
    # jev | small_model = only that step; off = always run the full extraction.
    request_router: str = "auto"

    # --- Background chat turns (app/api/v1/chat.py) ---
    # Hard wall-clock limit for one background LangGraph turn; on expiry the turn is
    # cancelled, marked failed and the "turn in progress" marker cleared.
    chat_turn_timeout_seconds: int = 900
    # false (default): POST /chat/sessions[/{id}/messages] returns 202 immediately and the turn
    # runs as a background task. true: turns run inline in the request (the pre-background
    # behaviour: 200/201 with the full result) — used by the test suite and for debugging. A
    # per-request `?wait=true|false` overrides it.
    chat_turns_inline: bool = False

    # --- AWS AgentCore Gateway web search (real-KPI-research tool for the chat scorecard
    # builder's propose_kpis node — see app/ai/web_search.py). A *separate* set of AWS
    # credentials from the main Bedrock ones above (a distinct Gateway-scoped IAM
    # principal), signed by hand with SigV4 (service "bedrock-agentcore") rather than via
    # boto3, since this Gateway endpoint has no botocore service model. All four left
    # unset (empty string) by default; web_search.py treats that as "not configured" and
    # returns no results rather than failing the chat turn — see infra/.env.example.
    agentcore_gateway_web_search_url: str = ""
    agentcore_gateway_web_search_tool_name: str = ""
    agentcore_gateway_aws_access_key_id: str = ""
    agentcore_gateway_aws_secret_access_key: str = ""

    # --- OpenRouter Jev quality gate (see app/ai/jev_client.py) ---
    # A SEPARATE provider/model from every other setting on this class: TypeSafe AI's
    # "Jev" ("System One" decision model), reached via OpenRouter's alpha Decisions API
    # (https://openrouter.ai/api/alpha/decisions — NOT the standard chat/completions
    # shape; see jev_client.py's module docstring for the live-confirmed request/response
    # contract). Used ONLY for the three quality-gate checkpoints added to
    # scorecard_builder.py (KPI-category planning, each category research agent's finding, and
    # the final per-turn answer) — every normal chat/judge LLM call stays on Bedrock
    # GLM-5/GLM-4.7-Flash above. Left unset (empty string) by default; jev_client.py
    # treats that as "not configured" and the quality-gate degrades to "passed" rather
    # than failing the chat turn — same graceful-degradation shape as the AgentCore
    # Gateway web-search settings above.
    openrouter_jev_api: str = ""
    # Pinned version (not the "~typesafe/jev-latest" rolling alias) — confirmed live
    # against this account's OpenRouter access on 2026-09-30 (see jev_client.py).
    openrouter_jev_model_id: str = "typesafe/jev-1.13"

    # --- AI evaluation pipeline (file / Google Drive evaluations; see docs/ai-eval-contract.md) ---
    # Master model for identify / digest / evidence selection / reasoning (Llama 4 Maverick profile).
    bedrock_master_model_id: str = "us.meta.llama4-maverick-17b-instruct-v1:0"
    # S3 bucket + Step Functions state machine written by aws/bootstrap into infra/.env. The APP keys
    # are a dedicated IAM user (S3 + states:* on one machine); they are NOT the Bedrock bearer token
    # and boto3 clients for S3/SFN are always built with them explicitly.
    s3_bucket: str = ""
    sfn_state_machine_arn: str = ""
    aws_app_access_key_id: str = ""
    aws_app_secret_access_key: str = ""
    # Max evaluations in ingesting/processing/scoring at once (clamped 1-5, see ai_eval_concurrency).
    ai_eval_max_concurrent: int = 3
    # true: no background dispatcher loop is started (tests / debugging); drive it with
    # `Dispatcher.run_until_idle()`. false (default): an asyncio loop runs inside the app lifespan.
    ai_eval_inline: bool = False
    ai_eval_poll_seconds: float = 4.0
    # Hard wall-clock ceiling for one evaluation (AWS stage + scoring), after which it fails `timeout`.
    ai_eval_timeout_seconds: int = 4 * 3600
    # Automatic re-runs of the scoring phase after a TRANSIENT failure (model unavailable / throttled
    # beyond the call-level retries). Scoring is idempotent, so a re-run is safe; waits grow between tries.
    ai_eval_scoring_retries: int = 2
    ai_eval_scoring_retry_delays: tuple[float, ...] = (20.0, 60.0)
    # Upload limits (browser -> S3 multipart). 2 GiB per file, 10 files per submission.
    upload_max_bytes: int = 2 * 1024**3
    upload_max_files: int = 10
    upload_part_size: int = 32 * 1024 * 1024
    upload_url_ttl_seconds: int = 900

    # Per-user cap on bytes uploaded for new evaluations in a rolling 24 h (checked when upload URLs are issued).
    upload_user_daily_bytes: int = 20 * 1024**3

    # --- Authentication (app/auth/*) ---
    # HS256 signing key for the 15-minute access JWT. No default: production refuses to start without a strong one;
    # outside production an unset value becomes an EPHEMERAL random key (sessions end on restart and differ between
    # processes, so set it in .env). Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    # Refresh token lifetime: "Remember me" gives a persistent cookie for `refresh_days_remember`; otherwise a
    # session cookie, which the server also expires after `refresh_hours_session`.
    refresh_days_remember: int = 30
    refresh_hours_session: int = 24
    # Two refreshes with the same token within this many seconds (two tabs) are not treated as token theft.
    refresh_reuse_grace_seconds: int = 10
    # Cookie flags. `cookie_secure` None = secure whenever ENV=production.
    cookie_secure: bool | None = None
    cookie_samesite: str = "lax"
    cookie_domain: str = ""
    # Used in the links emailed to users (reset password / verify email) and for the CSRF Origin check.
    frontend_url: str = "http://localhost:3000"
    # Password policy (OWASP): length over composition rules; max stops hash-DoS.
    password_min_length: int = 12
    password_max_length: int = 128
    # Account lockout after repeated bad passwords: `lockout_threshold` failures lock the account for
    # `lockout_base_minutes`, doubling for each further failure up to `lockout_max_minutes`.
    lockout_threshold: int = 5
    lockout_base_minutes: int = 5
    lockout_max_minutes: int = 60
    password_reset_ttl_minutes: int = 60
    email_verify_ttl_hours: int = 48
    # Rate limits ("count/period", the `limits` library syntax). Strict per-IP limits on the auth routes,
    # per-user limits on the expensive ones. Backed by Redis when REDIS_URL is set, in-memory otherwise.
    rate_limit_enabled: bool = True
    rate_limit_login: str = "10/minute"
    rate_limit_signup: str = "5/minute"
    rate_limit_forgot: str = "5/hour"
    rate_limit_reset: str = "10/hour"
    rate_limit_refresh: str = "60/minute"
    rate_limit_chat: str = "30/minute"
    rate_limit_ai_jobs: str = "20/minute"
    rate_limit_uploads: str = "30/minute"
    rate_limit_export: str = "10/minute"
    # Transactional email. "log" (default) prints the link to the server log; "ses" sends through Amazon SES.
    email_backend: str = "log"
    email_from: str = ""
    # OAuth sign-in. A provider is enabled only when BOTH its client id and secret are set; until then its
    # button stays disabled in the UI and the start endpoint answers 501.
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    oauth_microsoft_client_id: str = ""
    oauth_microsoft_client_secret: str = ""
    # Public base URL of the BROWSER-facing app, used to build the OAuth redirect URI:
    # {oauth_redirect_base}/api/v1/auth/oauth/{provider}/callback
    oauth_redirect_base: str = "http://localhost:3000"
    # --- First admin / user provisioning (app/auth/bootstrap.py) ---
    # NON-production only: when both are set, the api and worker create this admin on startup if no user has the
    # email yet (never touches an existing user's password). IGNORED - with a warning - when ENV=production.
    admin_email: str = ""
    admin_password: SecretStr = SecretStr("")
    # NON-production only: the sign-in identifier `ADMIN_USERNAME` (default "admin") is accepted in place of an email
    # and resolves to the ADMIN_EMAIL account, so a dev can sign in as `admin` / <ADMIN_PASSWORD>. Never in production.
    admin_username: str = "admin"
    # PRODUCTION first run: POST /api/v1/auth/register-user with this value in `X-Bootstrap-Token` creates the first
    # admin while zero users exist, then the endpoint is closed for good. Unset = bootstrap disabled. Remove it from
    # the environment once the admin exists.
    bootstrap_token: SecretStr = SecretStr("")
    # Public self-service signup (/auth/signup + OAuth account creation). None = on outside production, OFF in
    # production (accounts are then created by an admin through /auth/register-user).
    signup_enabled: bool | None = None
    rate_limit_register_user: str = "5/minute"
    # Admin accounts get a stricter minimum password length than ordinary users.
    admin_password_min_length: int = 14
    # Interactive API docs are served only outside production.
    docs_enabled: bool | None = None

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    @property
    def dev_username_login(self) -> bool:
        """True only outside production, with an ADMIN_EMAIL to resolve to. Production always requires an email."""
        return (not self.is_production) and bool(self.admin_email.strip()) and bool(self.admin_username.strip())

    @property
    def cookies_secure(self) -> bool:
        return self.is_production if self.cookie_secure is None else self.cookie_secure

    @property
    def docs_on(self) -> bool:
        return (not self.is_production) if self.docs_enabled is None else self.docs_enabled

    @property
    def signup_allowed(self) -> bool:
        return (not self.is_production) if self.signup_enabled is None else self.signup_enabled

    @model_validator(mode="after")
    def _ephemeral_dev_secret(self) -> Settings:
        if not self.jwt_secret and not self.is_production:
            self.jwt_secret = secrets.token_urlsafe(48)
            logger.warning(
                "JWT_SECRET is not set: using an EPHEMERAL random signing key (sessions end on restart and are not "
                "shared between processes). Set JWT_SECRET in .env; production refuses to start without it."
            )
        return self

    def validate_production_settings(self) -> None:
        """Refuse to boot a production process with missing / default / weak secrets or unsafe settings.

        Also warns when env-based admin credentials are present, because they are ignored in production."""
        if not self.is_production:
            return
        problems: list[str] = []
        secret = self.jwt_secret
        if len(secret) < 32 or _weak(secret):
            problems.append("JWT_SECRET must be a random value of at least 32 characters")
        if not self.database_url:
            problems.append("DATABASE_URL is not set")
        else:
            db_password = urlsplit(self.database_url).password or ""
            if len(db_password) < 12 or _weak(db_password):
                problems.append("the database password in DATABASE_URL must be random, 12+ characters, not a default")
        if not self.cookies_secure:
            problems.append("COOKIE_SECURE must not be disabled in production")
        if self.docs_on:
            problems.append("DOCS_ENABLED must be off in production")
        if "cors_origins" not in self.model_fields_set or not self.cors_origin_list:
            problems.append("CORS_ORIGINS must be set explicitly")
        elif any(o == "*" or "localhost" in o or "127.0.0.1" in o for o in self.cors_origin_list):
            problems.append("CORS_ORIGINS must list only the real https origins (no '*' / localhost)")
        if "frontend_url" not in self.model_fields_set or "localhost" in self.frontend_url:
            problems.append("FRONTEND_URL must be set to the public https URL of the app")
        if self.email_backend == "log":
            problems.append("EMAIL_BACKEND=log would write password-reset links to the logs; use EMAIL_BACKEND=ses")
        token = self.bootstrap_token.get_secret_value()
        if token and len(token) < 32:
            problems.append("BOOTSTRAP_TOKEN must be at least 32 random characters")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
        if self.admin_email or self.admin_password.get_secret_value():
            logger.warning(
                "ADMIN_EMAIL / ADMIN_PASSWORD are set but IGNORED in production. Create the first admin with "
                "POST /api/v1/auth/register-user and BOOTSTRAP_TOKEN, then remove these variables."
            )

    @property
    def ai_eval_concurrency(self) -> int:
        return max(1, min(5, int(self.ai_eval_max_concurrent)))

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
