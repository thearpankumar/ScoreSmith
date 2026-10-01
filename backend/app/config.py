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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
