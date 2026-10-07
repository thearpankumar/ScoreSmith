from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import chat, evaluations, evaluations_ai, evaluations_export, kpi_nodes, scorecards, users

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(scorecards.router)
api_router.include_router(kpi_nodes.router)
# AI evaluation routes first: literal `/evaluations/ai/...` paths must win over `/evaluations/{id}`.
api_router.include_router(evaluations_ai.router)
api_router.include_router(evaluations_export.router)
api_router.include_router(evaluations.router)
api_router.include_router(chat.router)
