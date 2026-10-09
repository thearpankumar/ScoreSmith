from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, chat, evaluations, evaluations_ai, evaluations_export, kpi_nodes, oauth, scorecards

api_router = APIRouter()
# Public auth routes (signup / login / refresh / forgot / reset / OAuth) and the signed-in user's own profile.
api_router.include_router(auth.router)
api_router.include_router(oauth.router)
api_router.include_router(auth.me_router)
api_router.include_router(scorecards.router)
api_router.include_router(kpi_nodes.router)
# AI evaluation routes first: literal `/evaluations/ai/...` paths must win over `/evaluations/{id}`.
api_router.include_router(evaluations_ai.router)
api_router.include_router(evaluations_export.router)
api_router.include_router(evaluations.router)
api_router.include_router(chat.router)
