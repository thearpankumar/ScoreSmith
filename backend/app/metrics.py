"""Autoscaling signal and ECS scale-in protection for the worker fleet.

The signal is BACKLOG PER WORKER = (queued evaluations + active evaluations + chat turns waiting for a
worker) / live workers. Scale out when it stays above the target, scale in when it is near zero (target
tracking on a custom CloudWatch metric, or KEDA / any scaler reading the worker's `/metrics`).

- `backlog_snapshot()` computes it from Postgres (+ the Redis worker registry; without Redis the worker count
  falls back to the number of distinct lease holders, never below 1).
- `publish_backlog_metric()` logs it as a JSON line and, when `AUTOSCALE_METRIC_NAMESPACE` is set, puts
  `BacklogPerWorker` into CloudWatch. Every worker may call it; with Redis, one worker wins a short lock per
  interval so the metric is published once.
- `set_task_protection()` turns on ECS task scale-in protection while a worker holds evaluations / turns, so
  scale-in never picks a busy worker (it would only be adopted by another one after its lease expires).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
import uuid
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.limits.redis_semaphore import RedisUnavailable, gate
from app.models.chat_session import ChatSession
from app.models.enums import ACTIVE_EVALUATION_STATUSES, EvaluationStatus
from app.models.evaluation import Evaluation

logger = logging.getLogger(__name__)

_WORKERS_KEY = "qs:workers"
_PUBLISH_LOCK_KEY = "qs:metrics:publish-lock"


async def register_worker(worker_id: str) -> None:
    """Heartbeat into the Redis worker registry (no-op without Redis)."""
    now_ms = int(time.time() * 1000)
    horizon = int(get_settings().lease_heartbeat_seconds * 3 * 1000)
    try:
        await gate().run(lambda r: r.zadd(_WORKERS_KEY, {worker_id: now_ms}))
        await gate().run(lambda r: r.zremrangebyscore(_WORKERS_KEY, "-inf", now_ms - horizon))
    except RedisUnavailable:
        pass


async def unregister_worker(worker_id: str) -> None:
    try:
        await gate().run(lambda r: r.zrem(_WORKERS_KEY, worker_id))
    except RedisUnavailable:
        pass


async def _registered_workers() -> int | None:
    horizon = int(get_settings().lease_heartbeat_seconds * 3 * 1000)
    try:
        return int(await gate().run(lambda r: r.zcount(_WORKERS_KEY, int(time.time() * 1000) - horizon, "+inf")))
    except RedisUnavailable:
        return None


async def backlog_snapshot() -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        queued = (
            await db.execute(
                select(func.count()).select_from(Evaluation).where(Evaluation.status == EvaluationStatus.QUEUED)
            )
        ).scalar_one()
        active = (
            await db.execute(
                select(func.count()).select_from(Evaluation).where(Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES))
            )
        ).scalar_one()
        waiting_turns = (
            await db.execute(
                select(func.count()).select_from(ChatSession).where(
                    ChatSession.pending_turn_started_at.is_not(None),
                    ChatSession.turn_message.is_not(None),
                    ChatSession.turn_lease_owner.is_(None),
                )
            )
        ).scalar_one()
        holders = (
            await db.execute(
                select(func.count(func.distinct(Evaluation.lease_owner))).where(
                    Evaluation.status.in_(ACTIVE_EVALUATION_STATUSES), Evaluation.lease_owner.is_not(None)
                )
            )
        ).scalar_one()
    workers = await _registered_workers()
    workers = max(1, workers if workers is not None else int(holders))
    backlog = int(queued) + int(active) + int(waiting_turns)
    return {
        "queued_evaluations": int(queued),
        "active_evaluations": int(active),
        "waiting_chat_turns": int(waiting_turns),
        "workers": workers,
        "backlog_per_worker": round(backlog / workers, 3),
    }


def _put_cloudwatch(namespace: str, value: float) -> None:
    import boto3

    boto3.client("cloudwatch", region_name=get_settings().aws_region).put_metric_data(
        Namespace=namespace, MetricData=[{"MetricName": "BacklogPerWorker", "Value": value, "Unit": "Count"}]
    )


async def publish_backlog_metric() -> dict[str, Any] | None:
    """Computes + publishes the metric once (see module docstring). Never raises."""
    settings = get_settings()
    try:
        # With Redis, only the worker that wins this short lock publishes in a given interval.
        lock_ttl = max(1, int(settings.autoscale_metric_interval_seconds * 0.8))
        try:
            won = await gate().run(
                lambda r: r.set(_PUBLISH_LOCK_KEY, uuid.uuid4().hex, nx=True, ex=lock_ttl)
            )
            if not won:
                return None
        except RedisUnavailable:
            pass  # no Redis: every worker publishes the same cluster-wide value (harmless)
        snapshot = await backlog_snapshot()
        logger.info("backlog_per_worker=%s", snapshot["backlog_per_worker"], extra={"metric": snapshot})
        if settings.autoscale_metric_namespace:
            await asyncio.to_thread(
                _put_cloudwatch, settings.autoscale_metric_namespace, snapshot["backlog_per_worker"]
            )
        return snapshot
    except Exception:  # noqa: BLE001 - metrics must never take a worker down
        logger.warning("Could not publish the backlog metric.", exc_info=True)
        return None


def _put_task_protection(agent_uri: str, enabled: bool) -> None:
    body = json.dumps({"ProtectionEnabled": enabled, "ExpiresInMinutes": 120}).encode()
    request = urllib.request.Request(
        f"{agent_uri}/task-protection/v1/state", data=body, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=3).read()  # noqa: S310 - the ECS agent endpoint, set by ECS itself


async def set_task_protection(enabled: bool) -> None:
    """ECS scale-in protection for this task (a no-op outside ECS, i.e. without ECS_AGENT_URI)."""
    import os

    agent_uri = os.environ.get("ECS_AGENT_URI")
    if not agent_uri:
        return
    try:
        await asyncio.to_thread(_put_task_protection, agent_uri, enabled)
    except Exception:  # noqa: BLE001 - best effort
        logger.warning("Could not set ECS task protection=%s", enabled, exc_info=True)
