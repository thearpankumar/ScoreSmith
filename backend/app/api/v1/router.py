from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin_users,
    auth,
    chat,
    chat_shares,
    evaluations,
    evaluations_ai,
    evaluations_export,
    evaluations_page,
    kpi_nodes,
    notifications,
    oauth,
    scorecards,
    sharing,
    trash,
)

api_router = APIRouter()
# Public auth routes (signup / login / refresh / forgot / reset / OAuth) and the signed-in user's own profile.
api_router.include_router(auth.router)
api_router.include_router(oauth.router)
api_router.include_router(auth.me_router)
# The literal /scorecards/trash routes must be registered before /scorecards/{scorecard_id}.
api_router.include_router(trash.router)
api_router.include_router(scorecards.router)
api_router.include_router(sharing.router)
api_router.include_router(notifications.router)
api_router.include_router(notifications.slots_router)
api_router.include_router(admin_users.router)
api_router.include_router(kpi_nodes.router)
# AI evaluation routes first: literal `/evaluations/ai/...` paths must win over `/evaluations/{id}`.
api_router.include_router(evaluations_ai.router)
api_router.include_router(evaluations_export.router)
api_router.include_router(evaluations_page.router)
api_router.include_router(evaluations.router)
api_router.include_router(chat_shares.router)
api_router.include_router(chat.router)
