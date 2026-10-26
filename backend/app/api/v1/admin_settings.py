"""Product-wide settings an admin changes at runtime (`/api/v1/admin/settings`). Every route requires the admin
role (`require_admin` -> 403) and is rate limited per admin; changes are audit-logged.

`/ai-provider`: which AI provider (AWS Bedrock or OpenRouter) the product uses for chat, judging, evaluation scoring,
embeddings and the extraction Lambda. A provider whose credentials are missing on the server is locked (409). The
choice applies to new chat turns and evaluations; running jobs finish on the provider they started with.

`/chat-models`: which of the server's allowed OpenRouter chat models users may pick in the chat, and the default
(app/ai/chat_models.py). Only the chat assistant uses them; the builder and evaluations keep their own models."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import chat_models, providers
from app.ai.web_search import agentcore_configured, web_search_backend
from app.db import get_db
from app.deps import require_admin
from app.models.app_setting import AppSetting
from app.models.user import User
from app.ratelimit import rate_limit
from app.schemas.app_settings import (
    AiProviderSettingRead,
    AiProviderUpdate,
    ChatModelOptionRead,
    ChatModelsSetting,
    ChatModelsUpdate,
    ModelsByProvider,
    ProviderAvailability,
    ProviderModels,
    SettingActor,
    WebSearchStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/settings",
    tags=["admin"],
    dependencies=[Depends(rate_limit("admin", lambda s: s.rate_limit_admin, per_user=True))],
)


def _models(provider: str) -> ProviderModels:
    m = providers.models_for(provider)
    lam = providers.LAMBDA_MODELS[provider]
    return ProviderModels(
        chat=m.chat,
        judge=m.judge,
        master=m.master,
        embedding=m.embedding,
        builder=m.builder,
        vision=list(lam["vision"]),
        transcription=list(lam["transcription"]),
        video=list(lam.get("video", [])),
    )


async def _read(db: AsyncSession) -> AiProviderSettingRead:
    """The current state, read from the database (not the per-process cache) so the admin sees the saved value."""
    row = (await db.execute(select(AppSetting).where(AppSetting.key == providers.SETTING_KEY))).scalar_one_or_none()
    stored = providers.parse_provider(row.value) if row is not None else None
    effective = providers.effective_provider(stored)
    available = providers.availability()
    updated_by = None
    if row is not None and row.updated_by is not None:
        user = await db.get(User, row.updated_by)
        if user is not None:
            updated_by = SettingActor(id=str(user.id), name=user.name)
    chat_row = (
        await db.execute(select(AppSetting).where(AppSetting.key == chat_models.SETTING_KEY))
    ).scalar_one_or_none()
    choice = chat_models.effective_choice(chat_row.value if chat_row is not None else None)
    return AiProviderSettingRead(
        provider=stored,
        effective_provider=effective,
        available=ProviderAvailability(bedrock=available["bedrock"], openrouter=available["openrouter"]),
        models=ModelsByProvider(bedrock=_models("bedrock"), openrouter=_models("openrouter")),
        web_search=WebSearchStatus(agentcore=agentcore_configured(), effective=web_search_backend(effective)),
        chat_models=ChatModelsSetting(
            options=[ChatModelOptionRead(**chat_models.describe(m)) for m in chat_models.allowed_models()],
            enabled=choice.enabled,
            default=choice.default,
        ),
        updated_at=row.updated_at if row is not None else None,
        updated_by=updated_by,
    )


def _schedule_backfill(provider: str) -> None:
    """After a switch, embed the existing scorecards with the new provider's embedding model in the background
    (app/ai/embedding_backfill.py), so suggest-similar finds them. Never fails the request."""
    try:
        from app.ai.embedding_backfill import schedule_backfill

        schedule_backfill(providers.client_for(provider), reason="provider_switch")
    except Exception:  # noqa: BLE001 - the switch itself already succeeded
        logger.warning("Could not schedule the embedding backfill after switching to %s.", provider, exc_info=True)


@router.get("/ai-provider", response_model=AiProviderSettingRead)
async def get_ai_provider(
    db: AsyncSession = Depends(get_db), _admin: User = Depends(require_admin)
) -> AiProviderSettingRead:
    return await _read(db)


@router.put("/ai-provider", response_model=AiProviderSettingRead)
async def put_ai_provider(
    payload: AiProviderUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AiProviderSettingRead:
    try:
        _row, previous = await providers.set_provider(db, payload.provider, admin, request)
    except providers.ProviderLocked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if previous != payload.provider:
        _schedule_backfill(payload.provider)
    return await _read(db)


@router.put("/chat-models", response_model=AiProviderSettingRead)
async def put_chat_models(
    payload: ChatModelsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AiProviderSettingRead:
    try:
        await chat_models.save_choice(db, payload.enabled, payload.default, admin, request)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return await _read(db)
