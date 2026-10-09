"""Background worker process: `python -m app.worker`.

Runs the two kinds of long job that must not live in the HTTP workers - the AI-evaluation dispatcher (claims
queued evaluations under a lease, drives them through Step Functions + the scoring graph) and background chat
turns - plus a tiny HTTP server for the orchestrator:

    GET /health   liveness: 200 while the process loop is alive (also reports what it is running)
    GET /ready    readiness: 200 when Postgres answers `SELECT 1`, else 503
    GET /metrics  the autoscaling signal (backlog per worker) as JSON

Run as many replicas as the backlog needs; they coordinate only through Postgres (SKIP LOCKED claims, leases)
and, for the shared Bedrock/Jev/search limits, Redis. On SIGTERM/SIGINT the worker DRAINS: it stops claiming,
gives running chat turns `worker_drain_grace_seconds` to finish, then stops the rest and releases its
evaluation leases so another worker resumes them at once instead of after the lease expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time

# psycopg's async mode is incompatible with Windows' default ProactorEventLoop (see app/main.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from app import metrics  # noqa: E402
from app.ai.scorecard_builder import get_graph_manager  # noqa: E402
from app.api.v1.chat import get_turn_runner  # noqa: E402
from app.auth.bootstrap import ensure_env_admin_safe  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.limits.redis_semaphore import redis_ping  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.pipeline.dispatcher import get_dispatcher  # noqa: E402

logger = logging.getLogger("app.worker")


class WorkerState:
    def __init__(self) -> None:
        self.draining = False
        self.started_at = time.time()


async def _db_ready() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _status_line(code: int) -> str:
    return {200: "200 OK", 404: "404 Not Found", 503: "503 Service Unavailable"}[code]


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: WorkerState) -> None:
    try:
        request_line = (await asyncio.wait_for(reader.readline(), timeout=5.0)).decode("latin-1")
        path = request_line.split(" ")[1].split("?")[0] if request_line.count(" ") >= 2 else "/"
        dispatcher, runner = get_dispatcher(), get_turn_runner()
        code, body = 200, {}
        if path == "/health":
            body = {
                "status": "draining" if state.draining else "ok",
                "role": "worker",
                "worker_id": dispatcher.worker_id,
                "running_evaluations": sum(1 for t in dispatcher._tasks.values() if not t.done()),
                "running_chat_turns": runner.running(),
                "uptime_seconds": int(time.time() - state.started_at),
            }
        elif path == "/ready":
            ok = await _db_ready() and not state.draining
            code, body = (200, {"status": "ready"}) if ok else (503, {"status": "not ready"})
            body["redis"] = {None: "disabled", True: "up", False: "down"}[await redis_ping()]
        elif path == "/metrics":
            body = await metrics.backlog_snapshot()
        else:
            code, body = 404, {"detail": "Not found"}
        payload = json.dumps(body).encode()
        writer.write(
            f"HTTP/1.1 {_status_line(code)}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
        )
        await writer.drain()
    except Exception:  # noqa: BLE001 - a bad probe must not affect the worker
        logger.debug("health request failed", exc_info=True)
    finally:
        writer.close()


async def _background_loops(state: WorkerState) -> None:
    """Registry heartbeat, backlog metric and ECS scale-in protection."""
    settings = get_settings()
    dispatcher, runner = get_dispatcher(), get_turn_runner()
    last_publish = float("-inf")
    protected = False
    while True:
        try:
            await metrics.register_worker(dispatcher.worker_id)
            busy = any(not t.done() for t in dispatcher._tasks.values()) or runner.running() > 0
            if busy != protected or busy:  # while busy, keep refreshing the (expiring) protection
                await metrics.set_task_protection(busy)
                protected = busy
            if time.monotonic() - last_publish >= settings.autoscale_metric_interval_seconds:
                last_publish = time.monotonic()
                await metrics.publish_backlog_metric()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("worker housekeeping failed", exc_info=True)
        await asyncio.sleep(max(1.0, settings.lease_heartbeat_seconds))


async def run_worker() -> None:
    settings = get_settings()
    settings.validate_production_settings()  # a worker with default secrets must not boot in production either
    configure_logging()
    await ensure_env_admin_safe()  # non-production only; idempotent and advisory-locked across replicas
    state = WorkerState()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    # Open the LangGraph checkpointer before any turn runs (same reason as the API lifespan).
    try:
        await get_graph_manager().get_compiled_graph()
    except Exception:  # noqa: BLE001
        logger.warning("Could not pre-initialize the chat graph at startup.", exc_info=True)

    dispatcher, runner = get_dispatcher(), get_turn_runner()
    await dispatcher.start()
    await runner.start()
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, state), host="0.0.0.0", port=settings.worker_health_port  # noqa: S104
    )
    housekeeping = asyncio.create_task(_background_loops(state), name="worker-housekeeping")
    logger.info("Worker %s started (health on :%d).", dispatcher.worker_id, settings.worker_health_port)

    await stop.wait()

    logger.info("Shutdown signal received: draining.")
    state.draining = True
    runner.stop_claiming()
    deadline = time.monotonic() + settings.worker_drain_grace_seconds
    while runner.running() and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    housekeeping.cancel()
    await asyncio.gather(housekeeping, return_exceptions=True)
    await dispatcher.stop()  # stops claiming + drivers, releases evaluation leases
    await runner.stop()  # cancels turns that did not finish (recorded as "interrupted")
    await metrics.unregister_worker(dispatcher.worker_id)
    await metrics.set_task_protection(False)
    server.close()
    await server.wait_closed()
    await get_graph_manager().aclose()
    logger.info("Worker stopped.")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
