"""Optimistic concurrency for edits of shared charts.

A client may send `If-Match: <updated_at ISO timestamp of the entity it loaded>` with a PATCH. If the entity was
changed since (by a collaborator), the request is rejected with 409 `stale_edit` and the current `updated_at`, so two
editors never silently overwrite each other. Without the header nothing changes (scripts, older clients)."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, Request, status


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip().strip('"').replace("Z", "+00:00"))
    except ValueError:
        return None


def check_if_match(request: Request, entity: object, what: str = "This item") -> None:
    raw = request.headers.get("if-match")
    if not raw or raw.strip() == "*":
        return
    sent = _parse(raw)
    current: datetime = entity.updated_at  # type: ignore[attr-defined]
    if sent is None or abs((current - sent).total_seconds()) > 0.000_001:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "stale_edit",
                "message": f"{what} was changed by someone else since you opened it. Refresh to see their changes.",
                "updated_at": current.isoformat(),
            },
        )
