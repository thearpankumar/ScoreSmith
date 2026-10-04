"""Shared FastAPI dependencies.

`get_current_user` below is a DEV-ONLY auth stub. It trusts an `X-User-Id` header (a raw
user UUID) or a trivial `Authorization: Bearer <user-id>` token, and loads that user from
the DB. There is no signature verification, no token expiry, and no password/identity
check of any kind — this is explicitly NOT production auth. Real OIDC/OAuth2 is deferred
per the plan (see plan doc: "Security & deployability checklist" / "Explicitly deferred").
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.bedrock_client import BedrockClient, BedrockClientProtocol
from app.ai.jev_client import JevClient, JevClientProtocol
from app.ai.web_search import AgentCoreWebSearchClient, WebSearchClientProtocol
from app.db import get_db
from app.models.user import User
from app.pipeline.aws_jobs import AwsJobsProtocol, Boto3AwsJobs
from app.pipeline.jev_scorer import JevScoreClient, JevScoreClientProtocol

# Single process-lifetime BedrockClient. Construction is cheap/lazy (see
# app/ai/bedrock_client.py — the boto3 client itself is only created on first real call),
# so a module-level singleton is safe to share across requests.
_bedrock_client: BedrockClientProtocol = BedrockClient()

# Single process-lifetime AgentCoreWebSearchClient, mirroring _bedrock_client above.
# Construction is cheap/lazy too — it only reads config; no network/HTTP client is
# created until the first real .search() call (see app/ai/web_search.py). Safe (and
# correct) to construct even when the AGENTCORE_GATEWAY_* settings are unset: `.search()`
# then just returns [] rather than failing the chat turn.
_web_search_client: WebSearchClientProtocol = AgentCoreWebSearchClient()

# Single process-lifetime JevClient (OpenRouter-backed quality gate — see
# app/ai/jev_client.py), mirroring _bedrock_client/_web_search_client above. Cheap/lazy
# too — nothing is created until the first real `.rate_match()` call, and it's safe to
# construct even when OPENROUTER_JEV_API is unset: `quality_gate()` then just degrades
# every checkpoint to "passed" rather than failing a chat turn.
_jev_client: JevClientProtocol = JevClient()


# AI evaluation pipeline singletons (cheap/lazy: boto3 clients are only built on first real call, and
# always with the dedicated APP access keys - see app/pipeline/aws_jobs.py).
_aws_jobs: AwsJobsProtocol = Boto3AwsJobs()
_jev_score_client: JevScoreClientProtocol = JevScoreClient()


def get_aws_jobs() -> AwsJobsProtocol:
    """FastAPI dependency for the AI-evaluation upload endpoints; overridden in tests with `FakeAwsJobs`."""
    return _aws_jobs


def get_jev_score_client() -> JevScoreClientProtocol:
    return _jev_score_client


def get_bedrock_client() -> BedrockClientProtocol:
    """FastAPI dependency for the AI routers (app/api/v1/chat.py, scorecards.py's
    suggest-similar, evaluations.py's run). Overridden in tests with a `FakeBedrockClient`
    via `app.dependency_overrides` — see tests/conftest.py."""
    return _bedrock_client


def get_web_search_client() -> WebSearchClientProtocol:
    """FastAPI dependency for app/api/v1/chat.py's chat-builder endpoints, letting
    `propose_kpis` offer the model a real web_search tool. Overridden in tests with a
    `FakeWebSearchClient` via `app.dependency_overrides` — see tests/conftest.py."""
    return _web_search_client


def get_jev_client() -> JevClientProtocol:
    """FastAPI dependency for app/api/v1/chat.py's chat-builder endpoints, letting the
    three quality-gate checkpoints in scorecard_builder.py rate decisions via Jev.
    Overridden in tests with a `FakeJevClient` via `app.dependency_overrides` — see
    tests/conftest.py."""
    return _jev_client


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    authorization: str | None = Header(default=None),
) -> User:
    """DEV-ONLY stub auth dependency. See module docstring."""
    raw_id = x_user_id
    if raw_id is None and authorization and authorization.lower().startswith("bearer "):
        raw_id = authorization.split(" ", 1)[1].strip()

    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-User-Id header or Bearer token (dev auth stub).",
        )

    try:
        user_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id / bearer token must be a valid user UUID (dev auth stub).",
        ) from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user.")
    return user
