"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys

# psycopg's async mode is incompatible with Windows' default ProactorEventLoop. This only
# matters for local (non-Docker) dev on Windows; the Docker image runs Linux, where the
# default event loop already works. Must run before any async engine is created.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.scorecard_builder import get_graph_manager
from app.api.v1.router import api_router
from app.config import get_settings

# Without this, every module-level `logging.getLogger(__name__)` in app/ai/* (Bedrock
# unavailability, web_search query/result counts, similarity-search failures, ...) is
# silently dropped: Python's root logger defaults to WARNING with no handler attached,
# and uvicorn only configures its OWN "uvicorn"/"uvicorn.access" loggers, not the root
# logger the rest of this app's code uses. INFO is deliberate (not DEBUG) — the AI layer
# logs one INFO line per web_search call and per Bedrock Converse call, useful for
# understanding real chat-builder sessions in production logs without being noisy.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    # Cleanly close the LangGraph AsyncPostgresSaver's pooled connection on shutdown.
    await get_graph_manager().aclose()


app = FastAPI(
    title="Quality Scorecard System API",
    description="Phase 0 + Cycle 1 backend: data foundation and core CRUD API, "
    "plus the Cycle 1c AI core (chat scorecard builder, LLM judge, similarity search).",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(api_router, prefix="/api/v1")
