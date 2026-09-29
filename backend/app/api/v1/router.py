from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import chat, evaluations, kpi_nodes, scorecards, users

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(scorecards.router)
api_router.include_router(kpi_nodes.router)
api_router.include_router(evaluations.router)
api_router.include_router(chat.router)
