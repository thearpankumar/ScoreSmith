"""Which AI provider (AWS Bedrock or OpenRouter) the product uses, and the LLM client singletons for each.

An admin picks the provider product-wide in Settings (`PUT /api/v1/admin/settings/ai-provider`); the choice is
stored in `app_settings` under the key "ai_provider" as `{"provider": "bedrock" | "openrouter"}`. A provider can only
be chosen while its credentials exist in the server environment (`availability()`):

- **Bedrock** is available when `AWS_BEARER_TOKEN_BEDROCK` is longer than one character (docker compose prefixes the
  secret with "A", so a bare "A" means no key), when an access-key pair is set, or when profile / container / web
  identity credentials are present (`AWS_PROFILE`, `AWS_CONTAINER_CREDENTIALS_*_URI`, `AWS_WEB_IDENTITY_TOKEN_FILE`,
  which covers ECS task roles), or when botocore finds credentials in the shared AWS config (`~/.aws` default
  profile, SSO; resolved once per process). The EC2 instance metadata service is NOT probed (it stalls off EC2):
  `BEDROCK_AVAILABLE=true|false` overrides the detection (e.g. an EC2 instance role).
- **OpenRouter** is available when `OPENROUTER_API_KEY` is set and non-blank. It is never the Jev key
  (`OPENROUTER_JEV_API`), in either direction.

`effective_provider()` turns the stored choice into the one actually used: the stored provider while it is
available, otherwise the single available one (Bedrock when both are). With no credentials at all the app still
answers "bedrock" (`active_provider()`), whose calls then fail with the usual "AI unavailable" errors.

Call sites never pick model ids by provider themselves: they ask the client they were handed (`models_of(client)`),
so a job that started on one provider finishes on it even after an admin switches.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai.bedrock_client import BedrockClient, BedrockClientProtocol
from app.config import Settings, get_settings
from app.models.app_setting import AppSetting
from app.models.enums import AuditAction

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

ProviderName = Literal["bedrock", "openrouter"]
PROVIDERS: tuple[ProviderName, ...] = get_args(ProviderName)
SETTING_KEY = "ai_provider"
DISPLAY_NAMES: dict[str, str] = {"bedrock": "Bedrock", "openrouter": "OpenRouter"}

# How long a read of the stored provider is reused before the database is asked again.
CACHE_TTL_SECONDS = 5.0
_clock = time.monotonic  # patched by tests

# Informational only (shown in Settings): the extraction Lambda owns the real lists and their order.
LAMBDA_MODELS: dict[str, dict[str, list[str]]] = {
    "bedrock": {
        "vision": ["moonshotai.kimi-k2.5", "qwen.qwen3-vl-235b-a22b", "us.meta.llama4-maverick-17b-instruct-v1:0"],
        # Amazon Transcribe first: Bedrock's Voxtral rejects audio input since Oct 2026 (kept as the fallback)
        "transcription": ["amazon.transcribe", "mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"],
        # Video frames: keyframe contact sheets read by a cheap vision model (aws/lambdas/src/worker/frames.py)
        "video": ["us.amazon.nova-2-lite-v1:0", "qwen.qwen3-vl-235b-a22b"],
    },
    "openrouter": {
        "vision": ["qwen/qwen3.7-flash", "qwen/qwen3-vl-235b-a22b-instruct"],
        "transcription": ["mistralai/voxtral-small-24b-2507", "mistralai/voxtral-mini-3b-2507"],
        "video": ["qwen/qwen3.7-flash", "google/gemini-3.1-flash-lite"],
    },
}

# Names an earlier OpenRouter prototype used. They are ignored now; warn_legacy_env() reports any still set.
LEGACY_ENV_NAMES: tuple[str, ...] = (
    "LLM_PROVIDER",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_CHAT_MODEL_ID",
    "OPENROUTER_JUDGE_MODEL_ID",
    "OPENROUTER_MASTER_MODEL_ID",
    "OPENROUTER_EMBEDDING_MODEL_ID",
    "OPENROUTER_SEARCH_MODEL_ID",
    "OPENROUTER_REASONING_EFFORT",
    "OPENROUTER_WEB_SEARCH_ENGINE",
    "OR_S3_BUCKET",
    "OR_SFN_STATE_MACHINE_ARN",
    "OR_AWS_APP_ACCESS_KEY_ID",
    "OR_AWS_APP_SECRET_ACCESS_KEY",
)

_CREDENTIAL_SOURCE_VARS = (
    "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)


@dataclass(frozen=True)
class ModelSet:
    """The backend's model ids for one provider (the Lambda's vision / transcription models are separate).

    `chat` / `judge` / `master` serve evaluations and the other non-chat callers; `builder` / `builder_judge` serve
    the chat scorecard builder (its KPI research / drafting calls and its judge-tier helpers). On OpenRouter the
    builder runs on its own model (`OPENROUTER_KPI_MODEL`) while evaluations keep `OPENROUTER_AGENT_MODEL`."""

    chat: str
    judge: str
    master: str
    embedding: str
    builder: str
    builder_judge: str


class ProviderLocked(Exception):
    """An admin chose a provider whose credentials are not configured on the server (-> HTTP 409)."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{DISPLAY_NAMES.get(provider, provider)} is not available: {reason}")


# --- availability -------------------------------------------------------------------------------------------------


def _env(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def _bedrock_credentials_present(environ: Mapping[str, str]) -> bool:
    if len(_env(environ, "AWS_BEARER_TOKEN_BEDROCK")) > 1:  # compose sends "A" + secret: a bare "A" is no key
        return True
    if _env(environ, "AWS_ACCESS_KEY_ID") and _env(environ, "AWS_SECRET_ACCESS_KEY"):
        return True
    if any(_env(environ, name) for name in _CREDENTIAL_SOURCE_VARS):
        return True
    # Only for the real process environment: a test passing its own `environ` must not see the developer's ~/.aws.
    return environ is os.environ and shared_aws_credentials()


@functools.lru_cache(maxsize=1)
def shared_aws_credentials() -> bool:
    """Whether botocore's default chain finds credentials without the instance-metadata / container providers (shared
    credentials and config files, SSO). Resolved once per process; any error counts as "none"."""
    try:
        import botocore.session

        resolver = botocore.session.get_session().get_component("credential_provider")
        for name in ("iam-role", "container-role"):  # network probes; containers are covered by the env check
            try:
                resolver.remove(name)
            except Exception:  # noqa: BLE001 - provider absent in this botocore version
                pass
        return resolver.load_credentials() is not None
    except Exception:  # noqa: BLE001
        logger.debug("Could not resolve shared AWS credentials.", exc_info=True)
        return False


def unavailable_reason(
    provider: str, settings: Settings | None = None, environ: Mapping[str, str] | None = None
) -> str | None:
    """Why `provider` cannot be used right now, or None when it can."""
    settings = settings or get_settings()
    environ = os.environ if environ is None else environ
    if provider == "bedrock":
        if settings.bedrock_available is not None:
            return None if settings.bedrock_available else "BEDROCK_AVAILABLE is false on the server"
        if _bedrock_credentials_present(environ):
            return None
        return "no AWS Bedrock credentials on the server"
    if provider == "openrouter":
        return None if (settings.openrouter_api_key or "").strip() else "OPENROUTER_API_KEY is not set"
    return f"unknown provider {provider!r}"


def availability(
    settings: Settings | None = None, environ: Mapping[str, str] | None = None
) -> dict[ProviderName, bool]:
    return {p: unavailable_reason(p, settings, environ) is None for p in PROVIDERS}


def is_available(p: str) -> bool:
    return p in PROVIDERS and unavailable_reason(p) is None


# --- models -------------------------------------------------------------------------------------------------------


def _normalize(p: Any) -> ProviderName:
    return p if p in PROVIDERS else "bedrock"


def models_for(p: str) -> ModelSet:
    s = get_settings()
    if p == "openrouter":
        agent, kpi = s.openrouter_agent_model, s.openrouter_kpi_model
        return ModelSet(
            chat=agent, judge=agent, master=agent, embedding=s.openrouter_embed_model, builder=kpi, builder_judge=kpi
        )
    return ModelSet(
        chat=s.bedrock_chat_model_id,
        judge=s.bedrock_judge_model_id,
        master=s.bedrock_master_model_id,
        embedding=s.bedrock_embedding_model_id,
        builder=s.bedrock_chat_model_id,
        builder_judge=s.bedrock_judge_model_id,
    )


def provider_of(client: Any) -> ProviderName:
    """The provider a client talks to. Test fakes carry no `provider` attribute and count as Bedrock."""
    return _normalize(getattr(client, "provider", "bedrock"))


def models_of(client: Any) -> ModelSet:
    """The model ids to use with `client`: its own attributes, each falling back to the provider's configured
    default (so `FakeBedrockClient`, which has none, keeps answering with the Bedrock ids tests pin)."""
    defaults = models_for(provider_of(client))

    def pick(attr: str, default: str) -> str:
        value = getattr(client, attr, None)
        return value if isinstance(value, str) and value else default

    return ModelSet(
        chat=pick("chat_model_id", defaults.chat),
        judge=pick("judge_model_id", defaults.judge),
        master=pick("master_model_id", defaults.master),
        embedding=pick("embedding_model_id", defaults.embedding),
        builder=pick("builder_model_id", defaults.builder),
        builder_judge=pick("builder_judge_model_id", defaults.builder_judge),
    )


def embedding_model_of(client: Any) -> str | None:
    """The embedding model `client` produces vectors with; None for clients that do not say (test fakes), which
    means "no per-model filter and no backfill"."""
    value = getattr(client, "embedding_model_id", None)
    return value if isinstance(value, str) and value else None


# --- client singletons --------------------------------------------------------------------------------------------

_clients: dict[str, BedrockClientProtocol] = {}
_clients_lock = threading.Lock()


def client_for(p: str) -> BedrockClientProtocol:
    """The process-wide client for provider `p` (created lazily; construction never touches the network)."""
    p = _normalize(p)
    client = _clients.get(p)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(p)
        if client is None:
            if p == "openrouter":
                from app.ai.openrouter_client import OpenRouterClient

                client = OpenRouterClient()
            else:
                client = BedrockClient()
            _clients[p] = client
    return client


def reset_clients() -> None:
    """Drop the client singletons (tests; settings changed). The web-search singletons are reset too."""
    with _clients_lock:
        _clients.clear()
    from app.ai.web_search import reset_web_search_clients

    reset_web_search_clients()


# --- the stored choice --------------------------------------------------------------------------------------------

_cached_value: ProviderName | None = None
_cached_at: float | None = None


def parse_provider(value: Any) -> ProviderName | None:
    """The provider in a stored `app_settings.value` (`{"provider": ...}`), None when absent or unknown."""
    if isinstance(value, dict):
        value = value.get("provider")
    return value if value in PROVIDERS else None


def _remember(value: ProviderName | None) -> None:
    global _cached_value, _cached_at
    _cached_value, _cached_at = value, _clock()


def invalidate_cache() -> None:
    global _cached_value, _cached_at
    _cached_value, _cached_at = None, None


_refresh_lock: asyncio.Lock | None = None
_refresh_lock_loop: asyncio.AbstractEventLoop | None = None


def _refresh_guard() -> asyncio.Lock:
    """One lock per event loop: after the TTL only one coroutine reads the database, the others wait for its value
    instead of each taking another pooled connection."""
    global _refresh_lock, _refresh_lock_loop
    loop = asyncio.get_running_loop()
    if _refresh_lock is None or _refresh_lock_loop is not loop:
        _refresh_lock, _refresh_lock_loop = asyncio.Lock(), loop
    return _refresh_lock


async def stored_provider() -> ProviderName | None:
    """The provider an admin saved, or None when none was saved. Read with its own short session and cached for
    CACHE_TTL_SECONDS; on a database error the last known value (or None) is returned instead of raising."""
    if _cached_at is not None and _clock() - _cached_at < CACHE_TTL_SECONDS:
        return _cached_value
    async with _refresh_guard():
        if _cached_at is not None and _clock() - _cached_at < CACHE_TTL_SECONDS:
            return _cached_value  # refreshed by the coroutine that held the lock
        try:
            from app.db import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                raw = (
                    await session.execute(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 - never fail a request because the setting could not be read
            logger.warning("Could not read the stored AI provider; using the last known value.", exc_info=True)
            _remember(_cached_value)  # retry the database after the TTL, not on every call
            return _cached_value
        value = parse_provider(raw)
        _remember(value)
        return value


def effective_provider(stored: str | None) -> ProviderName | None:
    """The provider actually used: `stored` while it is available, else the single available one (Bedrock when
    both are), None when neither is."""
    avail = availability()
    if stored in PROVIDERS and avail[stored]:
        return stored  # type: ignore[return-value]
    if avail["bedrock"]:
        return "bedrock"
    if avail["openrouter"]:
        return "openrouter"
    return None


async def active_provider() -> ProviderName:
    """The effective provider for new work; "bedrock" when nothing is available. NEVER raises."""
    try:
        return effective_provider(await stored_provider()) or "bedrock"
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve the active AI provider; using Bedrock.", exc_info=True)
        return "bedrock"


async def active_client() -> BedrockClientProtocol:
    return client_for(await active_provider())


async def provider_for_job(stored: str | None) -> ProviderName:
    """The provider for a job that recorded `stored` when it started: that one while it is still available (a
    running evaluation finishes on the provider it began with), otherwise the current active provider."""
    if stored in PROVIDERS and is_available(stored):
        return stored  # type: ignore[return-value]
    return await active_provider()


async def set_provider(
    db: AsyncSession, provider: str, actor: Any, request: Request | None = None
) -> tuple[AppSetting, str | None]:
    """Save the admin's choice: upsert the row, write an audit entry and commit. Returns (row, previous stored
    value); when `provider` equals the previous value nothing is written (no audit). Raises ProviderLocked when
    `provider` is not available and ValueError for an unknown name."""
    from app.audit import audit

    if provider not in PROVIDERS:
        raise ValueError(f"unknown AI provider {provider!r}")
    reason = unavailable_reason(provider)
    if reason:
        raise ProviderLocked(provider, reason)
    actor_id: uuid.UUID | None = getattr(actor, "id", None)

    for attempt in range(2):
        stmt = select(AppSetting).where(AppSetting.key == SETTING_KEY).with_for_update()
        row = (await db.execute(stmt)).scalar_one_or_none()
        previous = parse_provider(row.value) if row is not None else None
        if row is not None and previous == provider:
            await db.commit()  # releases the row lock; nothing changed
            _remember(previous)
            return row, previous
        if row is None:
            row = AppSetting(key=SETTING_KEY, value={"provider": provider}, updated_by=actor_id)
            db.add(row)
            try:
                await db.flush()
            except IntegrityError:
                # Another admin inserted the row at the same moment: read it (locked) and update it instead.
                await db.rollback()
                if attempt:
                    raise
                continue
        else:
            row.value = {"provider": provider}
            row.updated_by = actor_id
        audit(
            db,
            request,
            actor_id=actor_id,
            entity_type="app_setting",
            entity_id=row.id,
            action=AuditAction.UPDATE,
            event="ai_provider_changed",
            diff={"key": SETTING_KEY, "from": previous, "to": provider},
        )
        await db.commit()
        await db.refresh(row)
        _remember(provider)  # type: ignore[arg-type]
        logger.info("AI provider changed from %s to %s.", previous or "(unset)", provider)
        return row, previous
    raise RuntimeError("unreachable")  # pragma: no cover


# --- legacy environment -------------------------------------------------------------------------------------------


def warn_legacy_env(environ: Mapping[str, str] | None = None) -> list[str]:
    """Log (names only, never values) any variables from the earlier OpenRouter prototype that are still set;
    they have no effect. Returns the names found."""
    environ = os.environ if environ is None else environ
    found = [name for name in LEGACY_ENV_NAMES if name in environ]
    if found:
        logger.warning(
            "Ignoring legacy AI provider settings (remove them from the environment / infra/.env): %s. The provider "
            "is chosen in Settings by an admin; see docs/configuration.md for the current OPENROUTER_* names.",
            ", ".join(found),
        )
    return found
