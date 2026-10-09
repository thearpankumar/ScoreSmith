"""One-off administrative CLI: make every existing chart (as an editor) and every existing chat (read-only share, with
its chart) available to ONE user, e.g. after a data hand-over.

    python -m app.scripts.share_everything --to arpankumar1119 [--dry-run]

Idempotent and non-destructive: it only ADDS collaborator / chat-share rows (never changes ownership), skips what the
person owns or already has, and writes an audit-log row per change. The handle is resolved like a sign-in handle
(username, or the part of the email before the @, when unique).

Each share also creates ONE in-app notification for the recipient ("<owner> shared ... with you", linking to the
chart or the shared chat). Notifications carry a dedupe key, so re-running the CLI never duplicates them, and a re-run
also fills in the notifications of shares an earlier run created before notifications existed."""

from __future__ import annotations

import argparse
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import select  # noqa: E402

from app import notifications as notif  # noqa: E402
from app.audit import audit  # noqa: E402
from app.auth.handles import find_user_by_handle  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.models.chat_session import ChatSession  # noqa: E402
from app.models.enums import AuditAction  # noqa: E402
from app.models.scorecard import Scorecard  # noqa: E402
from app.models.sharing import ChatShare, ScorecardActivity, ScorecardCollaborator  # noqa: E402
from app.models.user import User  # noqa: E402


async def _notify(db, target: User, charts: list[Scorecard]) -> int:
    """One notification per share made by this CLI (idempotent through the per-user dedupe key)."""
    added = 0
    by_admin = set(
        (
            await db.execute(
                select(ScorecardActivity.scorecard_id).where(
                    ScorecardActivity.action == "collaborator_joined",
                    ScorecardActivity.actor_id.is_(None),
                    ScorecardActivity.entity_id == target.id,
                )
            )
        ).scalars().all()
    )
    owners = {u.id: u.name for u in (await db.execute(select(User))).scalars().all()}
    for card in charts:
        if card.id not in by_admin or card.deleted_at is not None:
            continue
        added += await notif.add_notification(
            db, target.id, notif.CHART_SHARED, f"{owners.get(card.owner_id, 'Someone')} shared “{card.name}” with you",
            body="You were added as an editor of this chart.", data={"scorecard_id": str(card.id)},
            link=f"/charts/{card.id}", dedupe_key=f"sharedall:{card.id}:{target.id}",
        )
    shares = (
        await db.execute(
            select(ChatShare, ChatSession)
            .join(ChatSession, ChatSession.id == ChatShare.session_id)
            .where(ChatShare.recipient_id == target.id)
        )
    ).all()
    for share, chat in shares:
        added += await notif.add_notification(
            db, target.id, notif.CHAT_SHARED,
            f"{owners.get(share.shared_by, 'Someone')} shared a chat with you"
            + (" and its chart" if share.with_chart else ""),
            body=f"Chat: {chat.title or 'a chat'}", data={"session_id": str(chat.id), "with_chart": share.with_chart},
            link=f"/chat/shared/{chat.id}", dedupe_key=f"chatshare:{share.id}:{int(share.with_chart)}",
        )
    return added


async def share_everything(handle: str, *, dry_run: bool = False) -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        target = await find_user_by_handle(db, handle, active_only=True)
        if target is None:
            raise SystemExit(f"No active user matches {handle!r} (or the handle is ambiguous).")
        mine = select(ScorecardCollaborator.scorecard_id).where(ScorecardCollaborator.user_id == target.id)
        have_charts = set((await db.execute(mine)).scalars().all())
        charts = (await db.execute(select(Scorecard).where(Scorecard.owner_id != target.id))).scalars().all()
        added_charts = 0
        for card in charts:
            if card.id in have_charts:
                continue
            added_charts += 1
            if dry_run:
                continue
            db.add(
                ScorecardCollaborator(scorecard_id=card.id, user_id=target.id, role="editor", invited_by=card.owner_id)
            )
            db.add(
                ScorecardActivity(
                    scorecard_id=card.id, actor_id=None, actor_name="Administrator", action="collaborator_joined",
                    entity_type="user", entity_id=target.id,
                    summary=f"{target.name} was added to the chart by an administrator",
                )
            )
            audit(db, None, actor_id=None, entity_type="scorecard", entity_id=card.id, action=AuditAction.UPDATE,
                  event="share_everything", diff={"user_id": str(target.id)})
        shared_cards = have_charts | {c.id for c in charts}
        have_chats = set(
            (await db.execute(select(ChatShare.session_id).where(ChatShare.recipient_id == target.id))).scalars().all()
        )
        chats = (await db.execute(select(ChatSession).where(ChatSession.user_id != target.id))).scalars().all()
        added_chats = 0
        for chat in chats:
            if chat.id in have_chats:
                continue
            added_chats += 1
            if dry_run:
                continue
            db.add(
                ChatShare(
                    session_id=chat.id, recipient_id=target.id, shared_by=chat.user_id,
                    with_chart=chat.target_scorecard_id in shared_cards,
                )
            )
            audit(db, None, actor_id=None, entity_type="chat_session", entity_id=chat.id, action=AuditAction.UPDATE,
                  event="share_everything", diff={"user_id": str(target.id)})
        notified = 0 if dry_run else await _notify(db, target, charts)
        if not dry_run:
            await db.commit()
        return {
            "charts_added": added_charts,
            "chats_added": added_chats,
            "charts_total": len(charts),
            "chats_total": len(chats),
            "notifications_added": notified,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", required=True, help="username or email handle of the recipient")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(share_everything(args.to, dry_run=args.dry_run))
    print(("DRY RUN: " if args.dry_run else "") + ", ".join(f"{k}={v}" for k, v in result.items()))


if __name__ == "__main__":
    main()
