"""Schemas for the admin settings API (`/api/v1/admin/settings`, app/api/v1/admin_settings.py)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ProviderLiteral = Literal["bedrock", "openrouter"]


class ProviderModels(BaseModel):
    """The models one provider uses. `vision` / `transcription` are the extraction Lambda's, in fallback order
    (informational: the Lambda owns the real lists)."""

    chat: str
    judge: str
    master: str
    embedding: str
    vision: list[str]
    transcription: list[str]


class ProviderAvailability(BaseModel):
    bedrock: bool
    openrouter: bool


class ModelsByProvider(BaseModel):
    bedrock: ProviderModels
    openrouter: ProviderModels


class WebSearchStatus(BaseModel):
    # AgentCore Gateway keys are configured (it is then used whichever provider is active).
    agentcore: bool
    # What the chat agent's web_search tool uses with the effective provider.
    effective: Literal["agentcore", "openrouter", "none"]


class SettingActor(BaseModel):
    id: str
    name: str


class AiProviderSettingRead(BaseModel):
    # The admin's saved choice; None = never chosen.
    provider: ProviderLiteral | None
    # The provider new work actually uses (the saved one while available, else the available one); None only when
    # no provider has credentials at all.
    effective_provider: ProviderLiteral | None
    available: ProviderAvailability
    models: ModelsByProvider
    web_search: WebSearchStatus
    updated_at: datetime | None
    updated_by: SettingActor | None


class AiProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderLiteral
