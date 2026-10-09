"""Admin-only CLI: HARD-delete leftover test / anonymised accounts that own nothing.

    python -m app.scripts.purge_users --pattern 'e2e-%@example.com' --pattern 'deleted-%@deleted.invalid'    # dry run
    python -m app.scripts.purge_users --pattern 'e2e-%@example.com' --yes                                    # for real

The admin UI's "delete user" only ANONYMISES (the row stays as a tombstone). This tool removes the row itself, for
accounts that never owned anything. It is deliberately conservative:

* patterns are SQL LIKE patterns on the e-mail (case-insensitive) and must be specific: an `@`, a literal domain and a
  literal prefix of at least 3 characters in front of the first wildcard (so `%`, `%@example.com` or `a%` are refused);
* NEVER touched, whatever the pattern: admins, the `system` marker user, `*@qualityscorecard.local` seed / demo accounts
  and anyone whose e-mail is listed in `PROTECTED_EMAILS`;
* a candidate is REFUSED (and reported with the reason) when anything important references it: charts (trashed ones
  too), chart versions, evaluations or batches it ran, chat sessions, chat shares, collaborator rows or invitations that
  are not merely pending / declined / revoked;
* what is cleaned up with the row: refresh and password-reset tokens, OAuth links, notifications, idempotency keys,
  and non-accepted invitations (sent or received);
* audit integrity: `audit_log.actor_id` is `ON DELETE SET NULL` (the rows stay, no longer attributed - the database
  does that), and one `user_purged` tombstone row (id, e-mail domain, number of un-attributed audit rows) is written
  per purge; `scorecard_activity.actor_id` / `invited_by` are nulled the same way (the actor_name text stays).

Without `--yes` (or with `--dry-run`) nothing is changed: the list of who would be deleted / refused is printed."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from dataclasses import dataclass, field

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import delete, func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.audit import audit  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.models.auth import OAuthIdentity, PasswordResetToken, RefreshToken  # noqa: E402
from app.models.chat_session import ChatSession  # noqa: E402
from app.models.enums import AuditAction  # noqa: E402
from app.models.evaluation import Evaluation  # noqa: E402
from app.models.evaluation_batch import EvaluationBatch  # noqa: E402
from app.models.idempotency_key import IdempotencyKey  # noqa: E402
from app.models.notification import Notification  # noqa: E402
from app.models.scorecard import Scorecard  # noqa: E402
from app.models.scorecard_version import ScorecardVersion  # noqa: E402
from app.models.sharing import ChatShare, ScorecardCollaborator, ScorecardInvitation  # noqa: E402
from app.models.user import ROLE_ADMIN, User  # noqa: E402

PROTECTED_EMAILS = frozenset({"designer@qualityscorecard.local", "arpankumar1119@gmail.com"})
PROTECTED_DOMAINS = ("@qualityscorecard.local",)


class PatternError(ValueError):
    pass


def validate_pattern(pattern: str) -> str:
    p = pattern.strip().lower()
    if "@" not in p:
        raise PatternError(f"pattern {pattern!r} must contain an '@'")
    local, _, domain = p.rpartition("@")
    if not domain or re.search(r"[%_]", domain):
        raise PatternError(f"pattern {pattern!r}: the domain part must be literal (no % or _)")
    prefix = re.split(r"[%_]", local, maxsplit=1)[0]
    if len(prefix) < 3:
        raise PatternError(f"pattern {pattern!r}: needs a literal prefix of at least 3 characters before any wildcard")
    return p


@dataclass
class Candidate:
    id: uuid.UUID
    email: str
    refused: list[str] = field(default_factory=list)


@dataclass
class Report:
    would_delete: list[Candidate] = field(default_factory=list)
    refused: list[Candidate] = field(default_factory=list)
    deleted: int = 0
    audit_rows_unattributed: int = 0


def _protected(user: User) -> str | None:
    email = (user.email or "").lower()
    if user.role == ROLE_ADMIN:
        return "administrator"
    if user.role == "system":
        return "system marker user"
    if email in PROTECTED_EMAILS or email.endswith(PROTECTED_DOMAINS):
        return "protected account"
    return None


async def _blockers(db: AsyncSession, user: User) -> list[str]:
    uid = user.id

    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar_one())

    checks = {
        "owns charts": select(func.count()).select_from(Scorecard).where(Scorecard.owner_id == uid),
        "created chart versions": select(func.count()).select_from(ScorecardVersion).where(
            ScorecardVersion.created_by == uid
        ),
        "ran evaluations": select(func.count()).select_from(Evaluation).where(
            or_(Evaluation.owner_id == uid, Evaluation.evaluated_by == uid)
        ),
        "created evaluation batches": select(func.count()).select_from(EvaluationBatch).where(
            EvaluationBatch.created_by == uid
        ),
        "has chat sessions": select(func.count()).select_from(ChatSession).where(ChatSession.user_id == uid),
        "has chat shares": select(func.count()).select_from(ChatShare).where(
            or_(ChatShare.recipient_id == uid, ChatShare.shared_by == uid)
        ),
        "is a chart collaborator": select(func.count()).select_from(ScorecardCollaborator).where(
            ScorecardCollaborator.user_id == uid
        ),
        "has accepted invitations": select(func.count()).select_from(ScorecardInvitation).where(
            or_(ScorecardInvitation.invitee_id == uid, ScorecardInvitation.inviter_id == uid),
            ScorecardInvitation.status == "accepted",
        ),
    }
    return [why for why, stmt in checks.items() if await count(stmt)]


async def purge_users(patterns: list[str], *, dry_run: bool = True) -> Report:
    like = [validate_pattern(p) for p in patterns]
    if not like:
        raise PatternError("at least one --pattern is required")
    report = Report()
    async with AsyncSessionLocal() as db:
        users = (
            await db.execute(
                select(User).where(or_(*[func.lower(User.email).like(p) for p in like])).order_by(User.email)
            )
        ).scalars().all()
        for user in users:
            cand = Candidate(user.id, user.email)
            reason = _protected(user)
            if reason:
                cand.refused.append(reason)
            else:
                cand.refused.extend(await _blockers(db, user))
            (report.refused if cand.refused else report.would_delete).append(cand)
        if dry_run:
            return report
        for cand in report.would_delete:
            user = await db.get(User, cand.id)
            n_audit = int(
                (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.actor_id == cand.id)))
                .scalar_one()
            )
            for model, column in (
                (RefreshToken, RefreshToken.user_id), (PasswordResetToken, PasswordResetToken.user_id),
                (OAuthIdentity, OAuthIdentity.user_id), (Notification, Notification.user_id),
            ):
                await db.execute(delete(model).where(column == cand.id))
            await db.execute(delete(IdempotencyKey).where(IdempotencyKey.user_id == cand.id))
            await db.execute(
                delete(ScorecardInvitation).where(
                    or_(ScorecardInvitation.invitee_id == cand.id, ScorecardInvitation.inviter_id == cand.id)
                )
            )
            domain = cand.email.rpartition("@")[2]
            audit(
                db, None, actor_id=None, entity_type="user", entity_id=cand.id, action=AuditAction.DELETE,
                event="user_purged", diff={"email_domain": domain, "audit_rows_unattributed": n_audit},
            )
            await db.delete(user)
            await db.flush()  # FK ON DELETE SET NULL un-attributes audit_log / scorecard_activity rows here
            report.audit_rows_unattributed += n_audit
            report.deleted += 1
        await db.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pattern", action="append", required=True, help="e-mail LIKE pattern, repeatable")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list only (the default)")
    mode.add_argument("--yes", action="store_true", help="actually delete")
    args = parser.parse_args()
    try:
        report = asyncio.run(purge_users(args.pattern, dry_run=not args.yes))
    except PatternError as exc:
        raise SystemExit(f"refused: {exc}") from exc
    print(("DELETING" if args.yes else "DRY RUN") + f": {len(report.would_delete)} account(s) qualify")
    for c in report.would_delete:
        print(f"  delete  {c.email}  ({c.id})")
    for c in report.refused:
        print(f"  KEEP    {c.email}  ({c.id}): {', '.join(c.refused)}")
    if args.yes:
        print(f"deleted={report.deleted} audit_rows_unattributed={report.audit_rows_unattributed}")


if __name__ == "__main__":
    main()
