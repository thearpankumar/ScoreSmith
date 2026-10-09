"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid

# psycopg's async mode is incompatible with Windows' default ProactorEventLoop. This only
# matters for local (non-Docker) dev on Windows; the Docker image runs Linux, where the
# default event loop already works. Must run before any async engine is created.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.ai.scorecard_builder import get_graph_manager
from app.api.v1.chat import get_turn_runner
from app.api.v1.router import api_router
from app.auth.bootstrap import ensure_env_admin_safe
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.limits.redis_semaphore import redis_ping
from app.logging_config import configure_logging
from app.pipeline.dispatcher import get_dispatcher

# Without this, every module-level `logging.getLogger(__name__)` in app/ai/* (Bedrock
# unavailability, web_search query/result counts, similarity-search failures, ...) is
# silently dropped: Python's root logger defaults to WARNING with no handler attached,
# and uvicorn only configures its OWN "uvicorn"/"uvicorn.access" loggers, not the root
# logger the rest of this app's code uses. INFO is deliberate (not DEBUG) — the AI layer
# logs one INFO line per web_search call and per Bedrock Converse call, useful for
# understanding real chat-builder sessions in production logs without being noisy.
# LOG_FORMAT=json switches to one JSON object per line carrying evaluation_id / session_id.
configure_logging()

settings = get_settings()
# Fails startup in production when a secret is still a default (JWT secret, DB password, insecure cookies, docs on).
settings.validate_production_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ROLE=api serves HTTP only: long jobs (evaluation drivers, background chat turns) are queued in Postgres
    # and run by `python -m app.worker` processes. ROLE=all (the default: local dev, tests) also runs them here.
    runs_jobs = settings.role != "api"
    # Non-production only: create the ADMIN_EMAIL admin if missing (advisory-locked, so replicas can race safely).
    await ensure_env_admin_safe()
    runner = get_turn_runner()
    if runs_jobs:
        # Turns whose lease expired belong to a process that died: record them as interrupted so the UI offers
        # a retry. (A live worker's turns keep their lease and are left alone.)
        await runner.start()
    # Open the LangGraph checkpointer (and create its tables on a fresh database) BEFORE any request
    # can run a turn. Lazily doing it on the first turn raced with that turn's own open DB
    # transaction: the checkpointer's `CREATE INDEX CONCURRENTLY` waits for every older transaction
    # to finish, including the one held by the very background task that triggered it (a hang).
    # The API role needs it too: GET /chat/sessions/{id} reads the checkpointed state.
    try:
        await get_graph_manager().get_compiled_graph()
    except Exception:  # noqa: BLE001 — DB not reachable/migrated yet: fall back to lazy init per request
        logging.getLogger(__name__).warning("Could not pre-initialize the chat graph at startup.", exc_info=True)
    # AI-evaluation queue: adopts evaluations whose lease expired, then claims queued ones (no-op when
    # AI_EVAL_INLINE, and not started at all for ROLE=api).
    dispatcher = get_dispatcher()
    if runs_jobs:
        await dispatcher.start()
    yield
    if runs_jobs:
        await dispatcher.stop()
        # Cancel still-running background chat turns (each records "interrupted").
        await runner.stop()
    # Cleanly close the LangGraph AsyncPostgresSaver's pooled connection on shutdown.
    await get_graph_manager().aclose()


app = FastAPI(
    title="Quality Scorecard System API",
    description="Phase 0 + Cycle 1 backend: data foundation and core CRUD API, "
    "plus the Cycle 1c AI core (chat scorecard builder, LLM judge, similarity search).",
    version="0.1.0",
    lifespan=lifespan,
    # The interactive docs (and the schema they read) are served outside production only.
    docs_url="/docs" if settings.docs_on else None,
    redoc_url="/redoc" if settings.docs_on else None,
    openapi_url="/openapi.json" if settings.docs_on else None,
)

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
# A JSON API: nothing it returns should ever be framed, sniffed, cached by shared caches or run as a page.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


@app.middleware("http")
async def _request_context_and_security_headers(request: Request, call_next):
    """Assigns a request id (echoed as X-Request-ID, stored on audit rows) and adds the security headers."""
    request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    if not request.url.path.startswith(_DOC_PATHS):
        response.headers.setdefault("Content-Security-Policy", _API_CSP)
    if request.url.path.startswith("/api/v1/auth") or request.url.path.startswith("/api/v1/me"):
        response.headers.setdefault("Cache-Control", "no-store")
    if settings.cookies_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# Added after the middleware above so CORS wraps it (outermost): preflights and error responses carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,  # explicit origins only: never "*" together with credentials
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "Idempotency-Key", "X-Request-ID"],
    # Lets the browser read the download filename / export counts on the cross-origin Excel export.
    expose_headers=["Content-Disposition", "X-Export-Count", "X-Export-Skipped", "X-Request-ID", "Retry-After"],
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
    """Liveness: the process is up. Deliberately does not touch the database."""
    return {"status": "ok"}


@app.get("/ready", tags=["health"])
async def ready() -> JSONResponse:
    """Readiness: the process can serve traffic (Postgres answers). Redis is reported but never required -
    the shared LLM limits fail open without it."""
    try:
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2.0)
    except Exception:  # noqa: BLE001 - any failure means "do not route traffic here"
        logging.getLogger(__name__).warning("Readiness check failed: database unreachable.", exc_info=True)
        return JSONResponse(status_code=503, content={"status": "not ready", "database": "down"})
    redis_state = {None: "disabled", True: "up", False: "down"}[await redis_ping()]
    return JSONResponse(content={"status": "ready", "role": settings.role, "database": "up", "redis": redis_state})


app.include_router(api_router, prefix="/api/v1")
