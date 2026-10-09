"""Shared seeding / polling helpers for the AI-evaluation pipeline tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy_utils import Ltree

from app.db import AsyncSessionLocal
from app.models.enums import EvaluationStatus
from app.models.evaluation import Evaluation
from app.models.evaluation_source import EvaluationSource
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User


async def seed_scorecard(db, kpi_names: tuple[str, ...] = ("Innovation", "Execution")):
    """A user, a scorecard with a current version and equally weighted leaf KPIs with 11 guideline levels."""
    owner = User(email=f"ai-{uuid.uuid4().hex[:8]}@example.com", name="AI Owner")
    db.add(owner)
    await db.flush()
    scorecard = Scorecard(name="AI Scorecard", owner_id=owner.id, domain="Hackathon")
    db.add(scorecard)
    await db.flush()
    version = ScorecardVersion(scorecard_id=scorecard.id, version_number=1, created_by=owner.id)
    db.add(version)
    await db.flush()
    scorecard.current_version_id = version.id
    weight = 100 / len(kpi_names)
    nodes = []
    for i, name in enumerate(kpi_names):
        nid = uuid.uuid4()
        node = KpiNode(
            id=nid, scorecard_version_id=version.id, parent_id=None, path=Ltree(nid.hex), level=1, name=name,
            weight=weight, display_order=i,
        )
        db.add(node)
        nodes.append(node)
    await db.flush()
    for node in nodes:
        for level in range(11):
            db.add(KpiGuideline(kpi_node_id=node.id, score_level=level, qualitative_text=f"{node.name} level {level}."))
    await db.commit()
    return owner, scorecard, version, nodes


async def seed_queued(db, owner, version, n: int = 1, *, status: EvaluationStatus = EvaluationStatus.QUEUED):
    base = datetime.now(UTC) - timedelta(minutes=5)
    out = []
    for i in range(n):
        ev = Evaluation(
            scorecard_version_id=version.id,
            name="AI evaluation",
            evaluated_by=owner.id,
            status=status,
            source_kind="upload",
            stage="queued",
            queued_at=base + timedelta(seconds=i),
            attempt=1,
        )
        db.add(ev)
        await db.flush()
        db.add(
            EvaluationSource(
                evaluation_id=ev.id, kind="upload", s3_key=f"uploads/u/g/{ev.id}.pdf", original_name="report.pdf",
                size=1000, status="pending",
            )
        )
        out.append(ev)
    await db.commit()
    return out


async def get_eval(eid) -> Evaluation:
    async with AsyncSessionLocal() as db:
        return await db.get(Evaluation, uuid.UUID(str(eid)))


async def wait_until(cond: Callable[[], Awaitable[bool] | bool], timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = cond()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def wait_status(eid, *statuses: EvaluationStatus, timeout: float = 30.0) -> Evaluation:
    async def ok() -> bool:
        return (await get_eval(eid)).status in statuses

    await wait_until(ok, timeout)
    return await get_eval(eid)


async def wait_execution_recorded(eid, timeout: float = 30.0) -> Evaluation:
    """The driver stores `sfn_execution_arn` AFTER the (fake) start call returned. Tests that simulate a crash must wait
    for it, otherwise the survivor legitimately starts a fresh execution instead of resuming the recorded one."""

    async def ok() -> bool:
        return (await get_eval(eid)).sfn_execution_arn is not None

    await wait_until(ok, timeout)
    return await get_eval(eid)
