"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertTriangle, Loader2, Plus, PanelLeftClose, PanelLeftOpen, Trash2 } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { UnsavedChangesDialog } from "./UnsavedChangesDialog";
import { useChatSessions } from "./ChatSessionsContext";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { usePersistedState } from "@/lib/usePersistedState";
import { deleteChatSession } from "@/lib/api-client";
import type { ChatSession } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";

const STORAGE_KEY = "qs.sessionListCollapsed";

const STATUS_VARIANT: Record<ChatSession["status"], "soft" | "muted" | "default"> = {
  active: "soft",
  completed: "default",
  abandoned: "muted",
};

export function SessionList({
  activeSessionId,
}: {
  activeSessionId: string;
}) {
  // Live session list + "does the current chat have unsent edits" flag — both shared via
  // context (see ChatSessionsContext) so they survive session-switch/new-chat navigation
  // instead of resetting to a server-fetched prop on every click (see app/chat/layout.tsx).
  const { sessions, removeSession, dirty } = useChatSessions();
  const { linkProps, isConfirmOpen, confirmLeave, cancelLeave } = useUnsavedChangesGuard(dirty);
  const [collapsed, setCollapsed] = usePersistedState(STORAGE_KEY, false);
  const bodyId = useId();
  const router = useRouter();

  // Hover-delete (Part B): a session pending confirmation, plus in-flight/error state for
  // the confirm dialog's own request — see confirmDelete below.
  const [pendingDelete, setPendingDelete] = useState<ChatSession | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteChatSession(pendingDelete.id);
      // Deleting the currently-open session: redirect to the next most recent one (or
      // the chat index if this was the last session left) — computed from the list as it
      // stood just before removal, so it never picks the session just deleted.
      const wasActive = pendingDelete.id === activeSessionId;
      const next = wasActive ? sessions.find((s) => s.id !== pendingDelete.id) : undefined;
      removeSession(pendingDelete.id);
      setPendingDelete(null);
      if (wasActive) router.push(next ? `/chat/${next.id}` : "/chat");
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : "Couldn't delete this chat. Try again.");
    } finally {
      setDeleting(false);
    }
  }

  const deleteDialog = (
    <Dialog open={!!pendingDelete} onOpenChange={(open) => !open && !deleting && setPendingDelete(null)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete “{pendingDelete?.title ?? "this chat"}”?</DialogTitle>
          <DialogDescription>
            This permanently deletes the chat and its messages. Any scorecard it already saved is not affected.
            This can&apos;t be undone.
          </DialogDescription>
        </DialogHeader>
        {deleteError && (
          <p role="alert" className="flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            {deleteError}
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => setPendingDelete(null)} disabled={deleting}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" onClick={confirmDelete} disabled={deleting}>
            {deleting && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  if (collapsed) {
    // Slim strip: same problem as NavRail (one card of content, the rest of the column
    // was dead space) — collapse down to just the essentials (new-scorecard + expand)
    // rather than a still-mostly-empty narrower list.
    return (
      <GlassCard elevation={1} className="flex h-full w-14 flex-col items-center gap-2 p-2">
        <Button asChild variant="default" size="icon" title="New scorecard">
          <Link {...linkProps("/chat/new")} aria-label="New scorecard">
            <Plus className="size-4" aria-hidden />
          </Link>
        </Button>
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          aria-expanded={false}
          aria-controls={bodyId}
          aria-label="Expand chat list"
          title="Expand chat list"
          className="mt-auto rounded-full p-1.5 text-ink-muted hover:bg-black/5 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
        >
          <PanelLeftOpen className="size-4" aria-hidden />
        </button>
        <UnsavedChangesDialog open={isConfirmOpen} onConfirmLeave={confirmLeave} onCancel={cancelLeave} />
        {deleteDialog}
      </GlassCard>
    );
  }

  return (
    <GlassCard elevation={1} className="flex h-full w-60 flex-col gap-2 p-3">
      <div className="flex items-center gap-1.5">
        <Button asChild variant="default" size="sm" className="flex-1">
          <Link {...linkProps("/chat/new")}>
            <Plus className="size-3.5" aria-hidden />
            New scorecard
          </Link>
        </Button>
        <button
          type="button"
          onClick={() => setCollapsed(true)}
          aria-expanded={true}
          aria-controls={bodyId}
          aria-label="Collapse chat list"
          title="Collapse chat list"
          className="shrink-0 rounded-full p-1.5 text-ink-muted hover:bg-black/5 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
        >
          <PanelLeftClose className="size-4" aria-hidden />
        </button>
      </div>

      <div id={bodyId} className="flex-1 space-y-1 overflow-y-auto thin-scrollbar">
        {sessions.map((session) => {
          const active = session.id === activeSessionId;
          return (
            // `group relative` wraps the row's Link + its hover-reveal delete button as
            // SIBLINGS (not delete-button-inside-Link) — nesting a <button> inside the
            // <a> a Link renders would be invalid, click-ambiguous HTML. See module notes
            // in ScorecardCard.tsx / the evaluations list for the same pattern.
            <div key={session.id} className="group relative">
              <Link
                {...linkProps(`/chat/${session.id}`)}
                className={cn(
                  "block rounded-xl px-3 py-2.5 pr-9 transition-colors",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
                  active ? "bg-white/80 shadow-sm" : "hover:bg-white/50",
                )}
              >
                <p className="truncate text-sm font-medium text-ink">{session.title}</p>
                <div className="mt-1 flex items-center justify-between gap-2">
                  <Badge variant={STATUS_VARIANT[session.status]} className="text-[10px]">
                    {session.status}
                  </Badge>
                  {/* Locale/timezone-dependent text: server and browser can legitimately
                      render different strings for the same instant. suppressHydrationWarning
                      is React's documented fix (see the hydration-mismatch error's own link)
                      for exactly this — it tells React to keep the client's version quietly
                      instead of discarding and remounting the whole tree. */}
                  <span className="text-[10px] text-ink-muted" suppressHydrationWarning>
                    {formatDateTime(session.lastActivityAt)}
                  </span>
                </div>
              </Link>
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setDeleteError(null);
                  setPendingDelete(session);
                }}
                aria-label={`Delete “${session.title}”`}
                title="Delete chat"
                className={cn(
                  "absolute right-1.5 top-1/2 -translate-y-1/2 rounded-full p-1.5 text-ink-muted",
                  "opacity-0 transition-opacity hover:bg-[var(--rag-poor)]/10 hover:text-[var(--rag-poor)]",
                  // Hover-reveal per current UX/a11y guidance: visible on hover AND on
                  // keyboard focus (group-focus-within / focus-visible — a hover-only
                  // affordance is unreachable by keyboard), and always visible on
                  // touch/coarse-pointer devices (no hover concept there at all).
                  "group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100",
                  "[@media(hover:none)]:opacity-100",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
                )}
              >
                <Trash2 className="size-3.5" aria-hidden />
              </button>
            </div>
          );
        })}
      </div>

      <UnsavedChangesDialog open={isConfirmOpen} onConfirmLeave={confirmLeave} onCancel={cancelLeave} />
      {deleteDialog}
    </GlassCard>
  );
}
