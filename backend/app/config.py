"""Application configuration, sourced from environment variables (.env in local dev)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Must be a psycopg (v3) URL: postgresql+psycopg://user:pass@host:port/db
    database_url: str = (
        "postgresql+psycopg://qs_app:qs_dev_password@localhost:5432/quality_scorecard"
    )

    # Comma-separated list of allowed CORS origins for the frontend.
    cors_origins: str = "http://localhost:3000"

    environment: str = "development"

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
    # Upload limits (browser -> S3 multipart). 2 GiB per file, 10 files per submission.
    upload_max_bytes: int = 2 * 1024**3
    upload_max_files: int = 10
    upload_part_size: int = 32 * 1024 * 1024
    upload_url_ttl_seconds: int = 900

    @property
    def ai_eval_concurrency(self) -> int:
        return max(1, min(5, int(self.ai_eval_max_concurrent)))

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
