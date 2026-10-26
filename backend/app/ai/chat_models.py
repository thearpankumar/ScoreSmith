"""Which models a user may pick for the chat assistant (app/ai/chat_agent.py), and the default.

Only with the OpenRouter provider: the server allows `OPENROUTER_CHAT_MODELS` (default: GPT-5 mini, Claude Sonnet
5.5, Claude Opus 5.5, GPT-5.6 Luna); an admin narrows that list and picks the default in Settings
(`PUT /api/v1/admin/settings/chat-models`), stored in `app_settings` under "chat_models" as
`{"enabled": [...], "default": "..."}`. A user picks one of the enabled models per message in the chat. The pick
only drives the assistant: the scorecard builder keeps `OPENROUTER_KPI_MODEL` and evaluations keep
`OPENROUTER_AGENT_MODEL`. With Bedrock there is nothing to pick (the assistant uses the Bedrock chat model).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models.app_setting import AppSetting
from app.models.enums import AuditAction

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SETTING_KEY = "chat_models"
CACHE_TTL_SECONDS = 5.0
_clock = time.monotonic  # patched by tests

# Display names for the known OpenRouter ids; an id not listed here is shown as itself.
CATALOGUE: dict[str, tuple[str, str]] = {
    "openai/gpt-5-mini": ("GPT-5 mini", "OpenAI"),
    "anthropic/claude-sonnet-5.5": ("Claude Sonnet 5.5", "Anthropic"),
    "anthropic/claude-opus-5.5": ("Claude Opus 5.5", "Anthropic"),
    "openai/gpt-5.6-luna": ("GPT-5.6 Luna", "OpenAI"),
}


@dataclass(frozen=True)
class ChatModelChoice:
    enabled: list[str]
    default: str | None


def describe(model_id: str) -> dict[str, str]:
    label, vendor = CATALOGUE.get(model_id, (model_id.split("/", 1)[-1], model_id.split("/", 1)[0].title()))
    return {"id": model_id, "label": label, "vendor": vendor}


def allowed_models() -> list[str]:
    """The server's allow-list (`OPENROUTER_CHAT_MODELS`), in order, without duplicates or blanks."""
    out: list[str] = []
    for raw in (get_settings().openrouter_chat_models or "").split(","):
        model = raw.strip()
        if model and "/" in model and model not in out:
            out.append(model)
    return out


def effective_choice(stored: Any) -> ChatModelChoice:
    """The stored admin choice reduced to what the server allows: enabled = stored ∩ allowed (all allowed when
    nothing was stored or nothing valid remains); default = the stored default when enabled, else
    `OPENROUTER_CHAT_DEFAULT_MODEL` when enabled, else the first enabled model."""
    allowed = allowed_models()
    stored = stored if isinstance(stored, dict) else {}
    wanted = stored.get("enabled")
    enabled = [m for m in allowed if isinstance(wanted, list) and m in wanted] if isinstance(wanted, list) else []
    if not enabled:
        enabled = allowed
    default = stored.get("default")
    if default not in enabled:
        configured = get_settings().openrouter_chat_default_model
        default = configured if configured in enabled else (enabled[0] if enabled else None)
    return ChatModelChoice(enabled=enabled, default=default)


_cached: tuple[float, Any] | None = None


def invalidate_cache() -> None:
    global _cached
    _cached = None


async def stored_value() -> Any:
    """The raw stored admin choice (None when never saved), cached for CACHE_TTL_SECONDS. Never raises: on a
    database error the last known value (or None) is used."""
    global _cached
    if _cached is not None and _clock() - _cached[0] < CACHE_TTL_SECONDS:
        return _cached[1]
    try:
        from app.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            value = (
                await session.execute(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - never fail a chat turn because the setting could not be read
        logger.warning("Could not read the chat model setting; using the last known value.", exc_info=True)
        value = _cached[1] if _cached is not None else None
    _cached = (_clock(), value)
    return value


async def current_choice() -> ChatModelChoice:
    return effective_choice(await stored_value())


async def resolve(provider: str, requested: str | None) -> str | None:
    """The model the chat assistant runs on for one message: `requested` when it is an enabled model, else the
    default. None with Bedrock (the assistant then uses the provider's chat model)."""
    if provider != "openrouter":
        return None
    choice = await current_choice()
    if requested and requested in choice.enabled:
        return requested
    return choice.default


async def save_choice(
    db: AsyncSession, enabled: list[str], default: str, actor: Any, request: Request | None = None
) -> ChatModelChoice:
    """Save the admin's choice (upsert + audit + commit). Raises ValueError when a model is not allowed by the server,
    the list is empty, or the default is not one of the enabled models."""
    from app.audit import audit

    allowed = allowed_models()
    unknown = [m for m in enabled if m not in allowed]
    if unknown:
        raise ValueError(f"not allowed on this server: {', '.join(unknown)}")
    enabled = [m for m in allowed if m in enabled]  # server order, no duplicates
    if not enabled:
        raise ValueError("enable at least one chat model")
    if default not in enabled:
        raise ValueError("the default model must be one of the enabled models")
    value = {"enabled": enabled, "default": default}
    actor_id: uuid.UUID | None = getattr(actor, "id", None)
    for attempt in range(2):
        row = (
            await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY).with_for_update())
        ).scalar_one_or_none()
        previous = row.value if row is not None else None
        if row is None:
            row = AppSetting(key=SETTING_KEY, value=value, updated_by=actor_id)
            db.add(row)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()  # another admin inserted it at the same moment: update theirs instead
                if attempt:
                    raise
                continue
        else:
            row.value = value
            row.updated_by = actor_id
        audit(
            db,
            request,
            actor_id=actor_id,
            entity_type="app_setting",
            entity_id=row.id,
            action=AuditAction.UPDATE,
            event="chat_models_changed",
            diff={"key": SETTING_KEY, "from": previous, "to": value},
        )
        await db.commit()
        invalidate_cache()
        return effective_choice(value)
    raise RuntimeError("unreachable")  # pragma: no cover
