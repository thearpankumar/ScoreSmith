"""KPI node (+ nested guideline) endpoints.

Weight-sum-to-100 is enforced by a Postgres DEFERRED CONSTRAINT TRIGGER on `kpi_nodes`
(see alembic/versions/0001_initial_schema.py), which only evaluates at transaction COMMIT.
Because each HTTP request here runs in its own transaction, a lone `POST /kpi-nodes` that
adds a single sibling to an incomplete group will still fail at commit unless that sibling
happens to complete the group to exactly 100. The practical way to create/rebalance a full
sibling group from this API is the bulk endpoints below, which insert/update the whole
group inside one transaction. Any trigger violation is caught and surfaced as 409.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy_utils import Ltree

from app.activity import log_edit
from app.authz import get_accessible_guideline, get_accessible_kpi_node, get_accessible_version
from app.db import get_db
from app.deps import get_current_user
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User
from app.occ import check_if_match
from app.schemas.kpi import (
    KpiGuidelineCreate,
    KpiGuidelineRead,
    KpiGuidelineUpdate,
    KpiNodeCreate,
    KpiNodeRead,
    KpiNodeReadWithGuidelines,
    KpiNodeUpdate,
)
from app.schemas.scorecard import ScorecardVersionReadWithKpiNodes

router = APIRouter(tags=["kpi-nodes"])


@router.get("/scorecard-versions/{version_id}", response_model=ScorecardVersionReadWithKpiNodes)
async def get_scorecard_version_flat(
    version_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> ScorecardVersion:
    """Additive (Wave 3 integration): a scorecard-version lookup that doesn't require
    already knowing its parent `scorecard_id` (the existing route is nested under
    `/scorecards/{scorecard_id}/versions/{version_id}`). The frontend needs this to
    resolve `evaluations.scorecard_version_id` -> its owning scorecard (an evaluation row
    only carries the version id), and to load a version's KPI tree directly from a chat
    session's `materialized_scorecard_version_id`."""
    await get_accessible_version(db, current_user, version_id)
    result = await db.execute(
        select(ScorecardVersion)
        .where(ScorecardVersion.id == version_id)
        .options(selectinload(ScorecardVersion.kpi_nodes).selectinload(KpiNode.guidelines))
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scorecard version not found.")
    return version


async def _scorecard_of_version(db: AsyncSession, version_id: uuid.UUID) -> uuid.UUID:
    return (
        await db.execute(select(ScorecardVersion.scorecard_id).where(ScorecardVersion.id == version_id))
    ).scalar_one()


async def _scorecard_of_node(db: AsyncSession, node: KpiNode) -> uuid.UUID:
    return await _scorecard_of_version(db, node.scorecard_version_id)


def _label_for(node_id: uuid.UUID) -> str:
    """A valid ltree label (letters/digits/underscores only) derived from the node's UUID."""
    return node_id.hex


async def _compute_path_and_level(
    db: AsyncSession, parent_id: uuid.UUID | None, node_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[str, int]:
    label = _label_for(node_id)
    if parent_id is None:
        return label, 1
    parent = await db.get(KpiNode, parent_id)
    if parent is None or parent.scorecard_version_id != version_id:  # a parent must belong to the SAME version
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="parent_id not found.")
    if parent.level >= 4:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Hierarchy depth cannot exceed 4 levels."
        )
    return f"{parent.path}.{label}", parent.level + 1


@router.get(
    "/scorecard-versions/{version_id}/kpi-nodes",
    response_model=list[KpiNodeReadWithGuidelines],
    tags=["kpi-nodes"],
)
async def list_kpi_nodes(
    version_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[KpiNode]:
    await get_accessible_version(db, current_user, version_id)
    result = await db.execute(
        select(KpiNode)
        .where(KpiNode.scorecard_version_id == version_id)
        .options(selectinload(KpiNode.guidelines))
        .order_by(KpiNode.level, KpiNode.display_order)
    )
    return list(result.scalars().all())


class KpiNodeBulkCreateItem(KpiNodeCreate):
    guidelines: list[KpiGuidelineCreate] = Field(default_factory=list)


class KpiNodeBulkCreateRequest(BaseModel):
    nodes: list[KpiNodeBulkCreateItem]


@router.post(
    "/scorecard-versions/{version_id}/kpi-nodes/bulk",
    response_model=list[KpiNodeReadWithGuidelines],
    status_code=status.HTTP_201_CREATED,
)
async def bulk_create_kpi_nodes(
    version_id: uuid.UUID,
    payload: KpiNodeBulkCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KpiNode]:
    """Create a full sibling group (plus their guidelines) in a single transaction, so the
    weight-sum-to-100 trigger evaluates once the whole group is present."""
    await get_accessible_version(db, current_user, version_id)
    created: list[KpiNode] = []
    for item in payload.nodes:
        node_id = uuid.uuid4()
        path, level = await _compute_path_and_level(db, item.parent_id, node_id, version_id)
        node = KpiNode(
            id=node_id,
            scorecard_version_id=version_id,
            parent_id=item.parent_id,
            path=Ltree(path),
            level=level,
            name=item.name,
            weight=item.weight,
            display_order=item.display_order,
            included_in_scoring=item.included_in_scoring,
        )
        db.add(node)
        for g in item.guidelines:
            db.add(KpiGuideline(kpi_node_id=node_id, **g.model_dump()))
        created.append(node)

    sc_id = await _scorecard_of_version(db, version_id)
    names = ", ".join(n.name for n in created[:3]) + (f" and {len(created) - 3} more" if len(created) > 3 else "")
    await log_edit(
        db, sc_id, current_user, "kpi_added", f"Added {len(created)} KPI(s): {names}",
        entity_type="kpi_node", entity_id=created[0].id if created else None,
        detail={"count": len(created), "names": [n.name for n in created][:20]},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Constraint violation (likely sibling weights do not sum to 100): {exc.orig}",
        ) from exc

    for node in created:
        await db.refresh(node, attribute_names=["guidelines"])
    return created


class KpiNodeWeightUpdate(BaseModel):
    id: uuid.UUID
    weight: float = Field(ge=0, le=100)


class KpiNodeWeightsBulkUpdateRequest(BaseModel):
    weights: list[KpiNodeWeightUpdate]


@router.patch("/kpi-nodes/weights", response_model=list[KpiNodeRead])
async def bulk_update_kpi_node_weights(
    payload: KpiNodeWeightsBulkUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KpiNode]:
    """Rebalance several sibling weights atomically (e.g. the whole sibling group)."""
    nodes: list[KpiNode] = []
    for item in payload.weights:
        node = await get_accessible_kpi_node(db, current_user, item.id)
        node.weight = item.weight
        nodes.append(node)
    if nodes:
        shown = ", ".join(f"{n.name} = {float(n.weight):g}" for n in nodes[:4])
        await log_edit(
            db, await _scorecard_of_node(db, nodes[0]), current_user, "weights_changed",
            f"Changed the weight of {len(nodes)} KPI(s): {shown}",
            entity_type="kpi_node", entity_id=nodes[0].id,
            detail={"weights": {n.name: float(n.weight) for n in nodes[:30]}},
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Constraint violation (sibling weights do not sum to 100): {exc.orig}",
        ) from exc
    for node in nodes:
        await db.refresh(node)
    return nodes


@router.get("/kpi-nodes/{node_id}", response_model=KpiNodeReadWithGuidelines)
async def get_kpi_node(
    node_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> KpiNode:
    await get_accessible_kpi_node(db, current_user, node_id)
    result = await db.execute(
        select(KpiNode).where(KpiNode.id == node_id).options(selectinload(KpiNode.guidelines))
    )
    node = result.scalar_one_or_none()
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="KPI node not found.")
    return node


@router.patch("/kpi-nodes/{node_id}", response_model=KpiNodeRead)
async def update_kpi_node(
    node_id: uuid.UUID,
    payload: KpiNodeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KpiNode:
    node = await get_accessible_kpi_node(db, current_user, node_id)
    check_if_match(request, node, "This KPI")
    changes = payload.model_dump(exclude_unset=True)
    old_name = node.name
    for field, value in changes.items():
        setattr(node, field, value)
    weight_only = set(changes) == {"weight"}
    summary = (
        f"Changed the weight of {node.name} to {float(node.weight):g}"
        if weight_only
        else f"Edited KPI {old_name} ({', '.join(sorted(changes))})"
    )
    await log_edit(
        db, await _scorecard_of_node(db, node), current_user,
        "weights_changed" if weight_only else "kpi_updated", summary,
        entity_type="kpi_node", entity_id=node.id, detail={"fields": sorted(changes)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Constraint violation (e.g. sibling weights no longer sum to 100): {exc.orig}",
        ) from exc
    await db.refresh(node)
    return node


@router.delete("/kpi-nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_kpi_node(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    node = await get_accessible_kpi_node(db, current_user, node_id)
    await log_edit(
        db, await _scorecard_of_node(db, node), current_user, "kpi_deleted", f"Deleted KPI {node.name}",
        entity_type="kpi_node", entity_id=node.id, detail={"name": node.name},
    )
    await db.delete(node)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Constraint violation deleting node (remaining siblings must still sum to 100, "
            f"unless this was the last sibling): {exc.orig}",
        ) from exc


# --- Guidelines, nested under a KPI node ---


@router.get("/kpi-nodes/{node_id}/guidelines", response_model=list[KpiGuidelineRead])
async def list_guidelines(
    node_id: uuid.UUID, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[KpiGuideline]:
    await get_accessible_kpi_node(db, current_user, node_id)
    result = await db.execute(
        select(KpiGuideline)
        .where(KpiGuideline.kpi_node_id == node_id)
        .order_by(KpiGuideline.score_level)
    )
    return list(result.scalars().all())


@router.post(
    "/kpi-nodes/{node_id}/guidelines",
    response_model=KpiGuidelineRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_guideline(
    node_id: uuid.UUID,
    payload: KpiGuidelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KpiGuideline:
    node = await get_accessible_kpi_node(db, current_user, node_id)
    guideline = KpiGuideline(kpi_node_id=node_id, **payload.model_dump())
    db.add(guideline)
    await db.flush()
    await log_edit(
        db, await _scorecard_of_node(db, node), current_user, "guideline_added",
        f"Added the level {payload.score_level} guideline of {node.name}",
        entity_type="kpi_guideline", entity_id=guideline.id,
        detail={"kpi": node.name, "score_level": payload.score_level},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Guideline already exists for this (kpi_node_id, score_level): {exc.orig}",
        ) from exc
    await db.refresh(guideline)
    return guideline


@router.patch("/kpi-nodes/{node_id}/guidelines/{guideline_id}", response_model=KpiGuidelineRead)
async def update_guideline(
    node_id: uuid.UUID,
    guideline_id: uuid.UUID,
    payload: KpiGuidelineUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> KpiGuideline:
    guideline = await get_accessible_guideline(db, current_user, node_id, guideline_id)
    check_if_match(request, guideline, "This guideline")
    node = await db.get(KpiNode, node_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(guideline, field, value)
    await log_edit(
        db, await _scorecard_of_node(db, node), current_user, "guideline_updated",
        f"Edited the level {guideline.score_level} guideline of {node.name}",
        entity_type="kpi_guideline", entity_id=guideline.id,
        detail={"kpi": node.name, "score_level": guideline.score_level},
    )
    await db.commit()
    await db.refresh(guideline)
    return guideline


@router.delete(
    "/kpi-nodes/{node_id}/guidelines/{guideline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_guideline(
    node_id: uuid.UUID,
    guideline_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    guideline = await get_accessible_guideline(db, current_user, node_id, guideline_id)
    node = await db.get(KpiNode, node_id)
    await log_edit(
        db, await _scorecard_of_node(db, node), current_user, "guideline_deleted",
        f"Deleted the level {guideline.score_level} guideline of {node.name}",
        entity_type="kpi_guideline", entity_id=guideline.id,
        detail={"kpi": node.name, "score_level": guideline.score_level},
    )
    await db.delete(guideline)
    await db.commit()
