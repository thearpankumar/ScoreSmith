"""`POST /evaluations/export` — download the selected evaluations as a styled Excel workbook."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models.user import User
from app.reporting.export_data import load_export_bundle
from app.reporting.workbook import ExportOptions, build_workbook
from app.reporting.xlsx_safety import clean_text, slugify_filename
from app.schemas.evaluation_export import EvaluationExportRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/export", response_class=Response, responses={200: {"content": {XLSX_MEDIA_TYPE: {}}}})
async def export_evaluations(
    payload: EvaluationExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # dev auth stub — see app/deps.py
) -> Response:
    """Builds the workbook (Summary with all insights first, Leaderboard when 2+ are exported, Evaluations, KPI
    matrices / detail, one sheet per student, reference sheets, Notes). Only evaluations that are completed with a
    final score are exported; deleted, failed, queued or unscored ones never appear in the workbook and are counted
    in `X-Export-Skipped` (and as one anonymous line on the Notes sheet)."""
    bundle = await load_export_bundle(db, payload.evaluation_ids)
    scored = [e for e in bundle.evaluations if e.is_scored]
    if not scored:
        if bundle.evaluations or bundle.excluded_count:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="None of the selected evaluations is completed with a final score; nothing to export.",
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="None of the selected evaluations exist.")

    generated_at = datetime.now(UTC)
    options = ExportOptions(
        include_reasoning=payload.include_reasoning,
        filter_summary=clean_text(payload.filter_summary, 300) if payload.filter_summary else None,
        generated_by=current_user.name,
        generated_at=generated_at,
    )
    content = await anyio.to_thread.run_sync(build_workbook, bundle, options)

    cards = {e.scorecard_name for e in scored}
    slug = slugify_filename(next(iter(cards))) if len(cards) == 1 else "multi"
    filename = f"evaluations_{slug}_{generated_at.strftime('%Y%m%d-%H%M')}.xlsx"
    skipped = len(bundle.missing_ids) + bundle.excluded_count + (len(bundle.evaluations) - len(scored))
    logger.info(
        "evaluation export: user=%s requested=%d found=%d scored=%d skipped=%d bytes=%d",
        current_user.id, len(payload.evaluation_ids), len(bundle.evaluations), len(scored), skipped, len(content),
    )
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Export-Count": str(len(scored)),
            "X-Export-Skipped": str(skipped),
        },
    )
