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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.ai.scorecard_builder import get_graph_manager
from app.api.v1.chat import recover_interrupted_turns, shutdown_background_turns
from app.api.v1.router import api_router
from app.config import get_settings
from app.pipeline.dispatcher import get_dispatcher

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
    # Turns that were running when the previous process died (crash/--reload) can never finish:
    # clear their "in progress" marker so the UI offers a retry (see app/api/v1/chat.py).
    recovered = await recover_interrupted_turns()
    if recovered:
        logging.getLogger(__name__).warning("Recovered %d chat turn(s) interrupted by a restart.", recovered)
    # Open the LangGraph checkpointer (and create its tables on a fresh database) BEFORE any request
    # can run a turn. Lazily doing it on the first turn raced with that turn's own open DB
    # transaction: the checkpointer's `CREATE INDEX CONCURRENTLY` waits for every older transaction
    # to finish, including the one held by the very background task that triggered it (a hang).
    try:
        await get_graph_manager().get_compiled_graph()
    except Exception:  # noqa: BLE001 — DB not reachable/migrated yet: fall back to lazy init per request
        logging.getLogger(__name__).warning("Could not pre-initialize the chat graph at startup.", exc_info=True)
    # AI-evaluation queue: resumes interrupted evaluations, then claims queued ones (no-op when AI_EVAL_INLINE).
    dispatcher = get_dispatcher()
    await dispatcher.start()
    yield
    await dispatcher.stop()
    # Cancel still-running background chat turns (each records "interrupted"), then cleanly close
    # the LangGraph AsyncPostgresSaver's pooled connection on shutdown.
    await shutdown_background_turns()
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
    # Lets the browser read the download filename / export counts on the cross-origin Excel export.
    expose_headers=["Content-Disposition", "X-Export-Count", "X-Export-Skipped"],
)


@app.exception_handler(IntegrityError)
async def _integrity_error_handler(_request: Request, exc: IntegrityError) -> JSONResponse:
    """A database constraint violation that an endpoint did not translate itself.

    Registered as a normal exception handler so it runs inside the CORS middleware: an UNHANDLED
    exception is answered by Starlette's outermost error middleware, which carries no CORS
    headers, and the browser then reports it as an opaque "NetworkError" instead of an API error.
    """
    logging.getLogger(__name__).warning("Unhandled database integrity error: %s", exc.orig)
    return JSONResponse(
        status_code=409,
        content={"detail": f"The request conflicts with existing data: {exc.orig}"},
    )


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(api_router, prefix="/api/v1")
