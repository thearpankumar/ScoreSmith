"""Audit trail: one `audit_log` row per security-relevant event (login, logout, reset, OAuth link) and per
create / update / delete / export of user data. Rows are added to the caller's session, so they commit (or roll
back) atomically with the change they describe."""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import client_ip
from app.models.audit_log import AuditLog
from app.models.enums import AuditAction


def request_context(request: Request | None) -> dict:
    if request is None:
        return {}
    return {
        "ip": client_ip(request),
        "user_agent": (request.headers.get("user-agent") or "")[:300] or None,
        "request_id": getattr(request.state, "request_id", None),
    }


def audit(
    db: AsyncSession,
    request: Request | None,
    *,
    actor_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    action: AuditAction,
    event: str | None = None,
    diff: dict | None = None,
) -> None:
    payload = dict(diff or {})
    if event:
        payload["event"] = event
    db.add(
        AuditLog(
            actor_id=actor_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            diff=payload or None,
            **request_context(request),
        )
    )
