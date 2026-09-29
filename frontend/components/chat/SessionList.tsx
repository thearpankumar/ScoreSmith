"use client";

import { useId } from "react";
import Link from "next/link";
import { Plus, PanelLeftClose, PanelLeftOpen } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { UnsavedChangesDialog } from "./UnsavedChangesDialog";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { usePersistedState } from "@/lib/usePersistedState";
import type { ChatSession } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";

const STORAGE_KEY = "qs.sessionListCollapsed";

const STATUS_VARIANT: Record<ChatSession["status"], "soft" | "muted" | "default"> = {
  active: "soft",
  completed: "default",
  abandoned: "muted",
};

export function SessionList({
  sessions,
  activeSessionId,
  dirty = false,
}: {
  sessions: ChatSession[];
  activeSessionId: string;
  /** True when the current chat has unsent live-preview edits — see ChatWorkspace's
   * `dirty`. Every navigation link this list renders (including to a DIFFERENT session,
   * which would otherwise silently discard those edits) is guarded when this is true. */
  dirty?: boolean;
}) {
  const { linkProps, isConfirmOpen, confirmLeave, cancelLeave } = useUnsavedChangesGuard(dirty);
  const [collapsed, setCollapsed] = usePersistedState(STORAGE_KEY, false);
  const bodyId = useId();

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
            <Link
              key={session.id}
              {...linkProps(`/chat/${session.id}`)}
              className={cn(
                "block rounded-xl px-3 py-2.5 transition-colors",
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
          );
        })}
      </div>

      <UnsavedChangesDialog open={isConfirmOpen} onConfirmLeave={confirmLeave} onCancel={cancelLeave} />
    </GlassCard>
  );
}
