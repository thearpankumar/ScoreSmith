"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArchiveRestore,
  Bell,
  CheckCheck,
  CheckCircle2,
  ClipboardCheck,
  Loader2,
  MessageCircleQuestion,
  MessageSquareShare,
  Pencil,
  RotateCcw,
  Save,
  Share2,
  Trash2,
  UserMinus,
  UserPlus,
  Users,
} from "lucide-react";

import { InvitePreview } from "@/components/sharing/InvitePreview";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useLiveStatus, requestLiveRefresh } from "@/components/live/LiveStatusProvider";
import {
  acceptInvitation,
  declineInvitation,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type NotificationItem,
} from "@/lib/collab-client";
import { timeAgo } from "@/lib/time";
import { cn } from "@/lib/utils";

const ICONS: Record<string, typeof Bell> = {
  invite_received: UserPlus,
  invite_accepted: Users,
  invite_declined: UserMinus,
  invite_revoked: UserMinus,
  collaborator_removed: UserMinus,
  collaborator_left: UserMinus,
  chart_edited: Pencil,
  evaluation_completed: ClipboardCheck,
  evaluation_failed: AlertTriangle,
  batch_completed: CheckCircle2,
  scorecard_saved: Save,
  chat_question: MessageCircleQuestion,
  chat_shared: MessageSquareShare,
  chart_shared: Share2,
  chart_trashed: Trash2,
  chart_restored: ArchiveRestore,
  evaluation_resumed: RotateCcw,
  evaluation_retry_available: RotateCcw,
};

/**
 * The bell in the top bar: unread badge (from the shared live-status poll), and a right-hand panel listing the user's
 * notifications - incoming invitations (preview / accept / decline right there), finished evaluations and batches,
 * saved scorecards, questions the chat assistant asked while away, and sharing events. Keyboard operable (it is a
 * button; the panel is a modal dialog with a focus trap), paginated ("Show older").
 */
export function NotificationBell() {
  const { unread, setUnread, refreshNow } = useLiveStatus();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        aria-haspopup="dialog"
        aria-expanded={open}
        data-testid="notification-bell"
        className="glass-tint glass-interactive relative flex size-11 shrink-0 items-center justify-center rounded-full text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
      >
        <Bell className="size-5" aria-hidden />
        {unread > 0 && (
          <span
            data-testid="notification-badge"
            className="absolute -right-0.5 -top-0.5 flex min-w-5 items-center justify-center rounded-full bg-[var(--rag-poor)] px-1 text-[11px] font-bold leading-5 text-white"
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent className="w-full max-w-md p-4 sm:p-6" aria-describedby="notif-desc">
          <SheetHeader className="pr-8">
            <SheetTitle>Notifications</SheetTitle>
            <SheetDescription id="notif-desc">Invitations, results and updates from your charts and chats.</SheetDescription>
          </SheetHeader>
          {open && <Panel onUnread={setUnread} onChanged={refreshNow} close={() => setOpen(false)} />}
        </SheetContent>
      </Sheet>
    </>
  );
}

function Panel({
  onUnread,
  onChanged,
  close,
}: {
  onUnread: (n: number) => void;
  onChanged: () => void;
  close: () => void;
}) {
  const router = useRouter();
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const alive = useRef(true);

  const load = useCallback(
    async (next: string | null) => {
      try {
        const page = await listNotifications({ cursor: next, limit: 15 });
        if (!alive.current) return;
        setItems((prev) => {
          const seen = new Set(prev.map((n) => n.id));
          return next ? [...prev, ...page.items.filter((n) => !seen.has(n.id))] : page.items;
        });
        setCursor(page.nextCursor);
        onUnread(page.unread);
        setError(null);
      } catch (err) {
        if (alive.current) setError(err instanceof Error ? err.message : "Could not load notifications.");
      } finally {
        if (alive.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [onUnread],
  );

  useEffect(() => {
    alive.current = true;
    void load(null);
    return () => {
      alive.current = false;
    };
  }, [load]);

  const unreadCount = items.filter((n) => !n.readAt).length;

  async function readOne(n: NotificationItem) {
    if (n.readAt) return;
    setItems((prev) => prev.map((x) => (x.id === n.id ? { ...x, readAt: new Date().toISOString() } : x)));
    try {
      onUnread(await markNotificationRead(n.id));
    } catch {
      /* the next poll corrects the count */
    }
  }

  async function open(n: NotificationItem) {
    void readOne(n);
    if (n.link && n.type !== "invite_received") {
      close();
      router.push(n.link);
    }
  }

  async function respond(n: NotificationItem, accept: boolean) {
    const id = String(n.data?.invitation_id ?? "");
    if (!id) return;
    setBusyId(n.id);
    try {
      if (accept) await acceptInvitation(id);
      else await declineInvitation(id);
      setNote(accept ? "Invitation accepted - the chart is now in your Charts list." : "Invitation declined.");
      resolveLocal(n.id, accept ? "accepted" : "declined");
      requestLiveRefresh();
      router.refresh();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Something went wrong.");
      await load(null);
    } finally {
      setBusyId(null);
    }
  }

  function resolveLocal(notificationId: string, status: string) {
    setItems((prev) =>
      prev.map((x) => (x.id === notificationId ? { ...x, invitationStatus: status, readAt: x.readAt ?? new Date().toISOString() } : x)),
    );
    onChanged();
  }

  async function markAll() {
    await markAllNotificationsRead().catch(() => undefined);
    setItems((prev) => prev.map((n) => ({ ...n, readAt: n.readAt ?? new Date().toISOString() })));
    onUnread(0);
  }

  const previewing = previewId ? items.find((n) => String(n.data?.invitation_id ?? "") === previewId) : undefined;

  if (previewId) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto">
        <InvitePreview
          invitationId={previewId}
          onBack={() => setPreviewId(null)}
          onResolved={(result) => {
            if (previewing) resolveLocal(previewing.id, result === "gone" ? "revoked" : result);
            setNote(result === "accepted" ? "Invitation accepted - the chart is now in your Charts list." : null);
            setPreviewId(null);
            requestLiveRefresh();
            router.refresh();
          }}
        />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-ink-muted" aria-live="polite">
          {loading ? "Loading…" : unreadCount > 0 ? `${unreadCount} unread` : "All caught up"}
        </span>
        <Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={markAll} disabled={unreadCount === 0}>
          <CheckCheck aria-hidden /> Mark all read
        </Button>
      </div>
      {note && (
        <p role="status" className="rounded-lg bg-lemon-soft px-3 py-2 text-xs text-lemon-ink">
          {note}
        </p>
      )}
      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}
      <ul className="-mx-1 flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto px-1 pb-2" aria-label="Notifications">
        {!loading && items.length === 0 && !error && (
          <li className="py-10 text-center text-sm text-ink-muted">Nothing here yet.</li>
        )}
        {items.map((n) => {
          const Icon = ICONS[n.type] ?? Bell;
          const pendingInvite = n.type === "invite_received" && n.invitationStatus === "pending";
          const invitationId = String(n.data?.invitation_id ?? "");
          return (
            <li
              key={n.id}
              className={cn(
                "flex gap-3 rounded-xl border border-hairline p-3",
                n.readAt ? "bg-white/40" : "bg-[var(--lemon-soft)]",
              )}
            >
              <span
                className={cn(
                  "mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-full",
                  n.type === "evaluation_failed" ? "bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]" : "bg-white/80 text-ink",
                )}
              >
                <Icon className="size-4" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  onClick={() => open(n)}
                  className="block min-h-11 w-full text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
                >
                  <span className="flex items-start gap-1.5">
                    {!n.readAt && <span className="mt-1.5 size-2 shrink-0 rounded-full bg-[var(--rag-poor)]" aria-label="Unread" />}
                    <span className="min-w-0 break-words text-sm font-semibold text-ink">{n.title}</span>
                  </span>
                  {n.body && <span className="mt-0.5 block break-words text-xs text-ink-muted">{n.body}</span>}
                  <span className="mt-1 block text-[11px] text-ink-muted">{timeAgo(n.createdAt)}</span>
                </button>
                {n.type === "invite_received" && n.invitationStatus && !pendingInvite && (
                  <p className="mt-1 text-xs font-medium capitalize text-ink-muted">{n.invitationStatus}</p>
                )}
                {pendingInvite && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => { void readOne(n); setPreviewId(invitationId); }}>
                      Preview
                    </Button>
                    <Button type="button" size="sm" className="min-h-11" disabled={busyId === n.id} onClick={() => respond(n, true)}>
                      {busyId === n.id && <Loader2 className="animate-spin" aria-hidden />} Accept
                    </Button>
                    <Button type="button" variant="ghost" size="sm" className="min-h-11" disabled={busyId === n.id} onClick={() => respond(n, false)}>
                      Decline
                    </Button>
                  </div>
                )}
              </div>
            </li>
          );
        })}
        {cursor && (
          <li className="py-1 text-center">
            <Button
              type="button"
              variant="ghost"
              className="min-h-11"
              disabled={loadingMore}
              onClick={() => {
                setLoadingMore(true);
                void load(cursor);
              }}
            >
              {loadingMore && <Loader2 className="animate-spin" aria-hidden />} Show older
            </Button>
          </li>
        )}
      </ul>
    </div>
  );
}
