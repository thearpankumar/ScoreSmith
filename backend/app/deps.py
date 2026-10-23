"""Shared FastAPI dependencies. `get_current_user` is the real authentication dependency (see app/auth/)."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import providers
from app.ai.bedrock_client import BedrockClientProtocol
from app.ai.jev_client import JevClient, JevClientProtocol
from app.ai.web_search import WebSearchClientProtocol, web_search_for
from app.auth.security import SAFE_METHODS, decode_access_token, extract_access_token, verify_csrf
from app.db import get_db
from app.models.user import ROLE_ADMIN, User
from app.pipeline.aws_jobs import AwsJobsProtocol, Boto3AwsJobs
from app.pipeline.jev_scorer import JevScoreClient, JevScoreClientProtocol

# The LLM clients (one per AI provider: Bedrock / OpenRouter) and the web-search clients are process singletons
# owned by app/ai/providers.py and app/ai/web_search.py; which one a request gets depends on the provider an admin
# chose in Settings (see get_bedrock_client below).

# Single process-lifetime JevClient (OpenRouter-backed quality gate — see
# app/ai/jev_client.py; its own OPENROUTER_JEV_API key, never OPENROUTER_API_KEY). Cheap/lazy
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


async def get_bedrock_client() -> BedrockClientProtocol:
    """FastAPI dependency for the AI routers (app/api/v1/chat.py, scorecards.py's suggest-similar,
    evaluations.py's run): the LLM client of the ACTIVE AI provider (Bedrock or OpenRouter, chosen by an admin -
    see app/ai/providers.py). Never raises. The name is kept for the tests, which override it with a
    `FakeBedrockClient` via `app.dependency_overrides` - see tests/conftest.py."""
    return await providers.active_client()


async def get_web_search_client(
    llm: BedrockClientProtocol = Depends(get_bedrock_client),
) -> WebSearchClientProtocol:
    """FastAPI dependency for app/api/v1/chat.py's chat-builder endpoints, letting
    `propose_kpis` offer the model a real web_search tool: AgentCore when configured, else OpenRouter's search
    when the request's LLM is OpenRouter (see app/ai/web_search.py::web_search_for). Overridden in tests with a
    `FakeWebSearchClient` via `app.dependency_overrides` — see tests/conftest.py."""
    return web_search_for(providers.provider_of(llm))


def get_jev_client() -> JevClientProtocol:
    """FastAPI dependency for app/api/v1/chat.py's chat-builder endpoints, letting the
    three quality-gate checkpoints in scorecard_builder.py rate decisions via Jev.
    Overridden in tests with a `FakeJevClient` via `app.dependency_overrides` — see
    tests/conftest.py."""
    return _jev_client


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """The signed-in user, from a 15-minute access JWT (httpOnly cookie for the browser, or an
    `Authorization: Bearer` header for scripts/tests).

    Cookie-authenticated unsafe requests (POST/PATCH/PUT/DELETE) must also pass the CSRF double-submit check;
    bearer-token requests are not ambient credentials and skip it. Raises 401 for a missing, malformed, expired
    or revoked token and for an unknown / deactivated user."""
    token, via_cookie = extract_access_token(request)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.", headers={"WWW-Authenticate": "Bearer"}
        )
    claims = decode_access_token(token)
    try:
        user_id = uuid.UUID(claims["sub"])
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.")
    issued_ms = int(claims.get("iatm") or int(claims["iat"]) * 1000)
    if user.sessions_valid_after is not None and issued_ms <= int(user.sessions_valid_after.timestamp() * 1000):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Session expired. Please sign in again.")
    if via_cookie and request.method not in SAFE_METHODS:
        verify_csrf(request)
    request.state.user_id = user.id
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """The signed-in user, who must have the admin role (403 otherwise)."""
    if user.role != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Administrator access required.")
    return user
