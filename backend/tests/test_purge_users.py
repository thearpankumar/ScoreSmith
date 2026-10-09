"""`python -m app.scripts.purge_users`: hard-deletes only unreferenced throw-away accounts; protected accounts and
accounts that own anything are refused; audit rows stay (un-attributed) and a tombstone is written."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.auth import RefreshToken
from app.models.enums import AuditAction
from app.models.notification import Notification
from app.models.scorecard import Scorecard
from app.models.sharing import ScorecardInvitation
from app.models.user import User
from app.scripts.purge_users import PatternError, purge_users, validate_pattern
from tests.sharing_world import make_chart


def _user(db: Session, email: str, **kw) -> User:
    u = User(email=email, name=email.split("@")[0], **kw)
    db.add(u)
    db.commit()
    return u


def _run(patterns: list[str], dry_run: bool):
    return asyncio.run(purge_users(patterns, dry_run=dry_run))


def test_patterns_must_be_specific() -> None:
    for bad in ("%", "%@example.com", "a%@example.com", "e2e-%@%", "e2e-%", "e2e-%@exa_ple.com"):
        with pytest.raises(PatternError):
            validate_pattern(bad)
    assert validate_pattern("E2E-%@Example.com") == "e2e-%@example.com"


def test_dry_run_lists_and_changes_nothing_then_yes_deletes_only_clean_test_accounts(db_session: Session) -> None:
    clean = _user(db_session, "e2e-clean@example.com")
    with_token = _user(db_session, "e2e-token@example.com")
    owner = _user(db_session, "e2e-owner@example.com")
    admin = _user(db_session, "e2e-admin@example.com", role="admin")
    seed = _user(db_session, "e2e-seed@qualityscorecard.local")
    tombstone = _user(db_session, "deleted-abc@deleted.invalid", is_active=False)
    bystander = _user(db_session, "someone@example.com")
    make_chart(db_session, owner)
    db_session.add(RefreshToken(user_id=with_token.id, family_id=uuid.uuid4(), token_hash="h" * 64,
                                expires_at=datetime.now(UTC) + timedelta(days=1)))
    db_session.add(Notification(user_id=with_token.id, type="x", title="t"))
    db_session.add(ScorecardInvitation(scorecard_id=db_session.query(Scorecard).first().id, inviter_id=owner.id,
                                       invitee_id=with_token.id, role="editor", status="declined"))
    db_session.add(AuditLog(actor_id=with_token.id, entity_type="user", entity_id=with_token.id,
                            action=AuditAction.UPDATE, diff={"event": "login"}))
    db_session.commit()
    ids = {u.id for u in (clean, with_token, owner, admin, seed, tombstone, bystander)}
    purged_ids = {clean.id, with_token.id, tombstone.id}
    bystander_id = bystander.id
    pats = ["e2e-%@example.com", "e2e-%@qualityscorecard.local", "deleted-%@deleted.invalid"]

    dry = _run(pats, True)
    assert {c.email for c in dry.would_delete} == {
        "e2e-clean@example.com", "e2e-token@example.com", "deleted-abc@deleted.invalid"
    }
    refused = {c.email: c.refused for c in dry.refused}
    assert refused["e2e-owner@example.com"] == ["owns charts", "created chart versions"]
    assert "administrator" in refused["e2e-admin@example.com"]
    assert refused["e2e-seed@qualityscorecard.local"] == ["protected account"]
    db_session.expire_all()
    assert {u.id for u in db_session.execute(select(User)).scalars()} == ids  # a dry run deletes nothing

    real = _run(pats, False)
    assert real.deleted == 3 and real.audit_rows_unattributed == 1
    db_session.expire_all()
    left = {u.email for u in db_session.execute(select(User)).scalars()}
    assert left == {"e2e-owner@example.com", "e2e-admin@example.com", "e2e-seed@qualityscorecard.local",
                    "someone@example.com"}
    assert db_session.execute(select(RefreshToken)).scalars().all() == []
    assert db_session.execute(select(Notification)).scalars().all() == []
    assert db_session.execute(select(ScorecardInvitation)).scalars().all() == []
    audit_rows = db_session.execute(select(AuditLog)).scalars().all()
    login = [a for a in audit_rows if (a.diff or {}).get("event") == "login"]
    assert len(login) == 1 and login[0].actor_id is None  # kept, no longer attributed
    tombs = [a for a in audit_rows if (a.diff or {}).get("event") == "user_purged"]
    assert len(tombs) == 3 and {t.entity_id for t in tombs} == purged_ids
    assert all("@" not in str(t.diff) for t in tombs)  # only the domain is kept, never the address

    again = _run(pats, False)  # idempotent
    assert again.deleted == 0
    assert db_session.get(User, bystander_id) is not None
