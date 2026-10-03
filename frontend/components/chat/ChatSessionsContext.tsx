"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { listChatSessions } from "@/lib/api-client";
import type { ChatSession } from "@/lib/types";

interface ChatSessionsContextValue {
  /** The live sidebar session list — seeded once from the server (see the root `app/layout.tsx`)
   * and kept up to date entirely client-side afterward via the setters below. */
  sessions: ChatSession[];
  /** Reflects a real (or changed) session title into the sidebar without a page reload —
   * used by ChatWorkspace's turn-status poll and its own turn responses (a brand-new
   * session's id + title only exist once its first turn resolves). `fallback` supplies
   * the fields a freshly-created session needs that a bare (id, title) pair doesn't carry. */
  upsertSessionTitle: (
    id: string,
    title: string | null | undefined,
    fallback: { userId: string; targetScorecardId: string | null },
  ) => void;
  /** Hover-delete (SessionList) — removes a session from the live list immediately on a
   * successful `DELETE /chat/sessions/{id}`, no page reload/refetch needed. */
  removeSession: (id: string) => void;
  /** True when the currently open chat (whichever ChatWorkspace instance is mounted) has
   * unsent live-preview edits — every navigation link the sidebar renders (including to a
   * DIFFERENT session) must be guarded while this is true. Set by ChatWorkspace itself via
   * `setDirty`, since the layout holding this context has no visibility into any one
   * chat's draft-editing state. */
  dirty: boolean;
  setDirty: (dirty: boolean) => void;
  /** Session ids the open chat knows are busy right now (its own turn, before/while the server
   * reports it) — merged with the server's `turnInProgress` flag for the sidebar badge. */
  busyIds: ReadonlySet<string>;
  setSessionBusy: (id: string, busy: boolean) => void;
}

const ChatSessionsContext = createContext<ChatSessionsContextValue | null>(null);

/**
 * Lives in the root `app/layout.tsx`, wrapping `AppShell` (and so both `NavRail` and
 * every routed page) — which, like any layout above a route segment, Next.js does NOT
 * unmount/remount on navigation between `app/chat/page.tsx` and
 * `app/chat/[sessionId]/page.tsx` (different route segments/params get fresh page
 * instances, but shared ancestor layouts persist — see Next.js's App Router
 * layout-persistence model). That makes this the one place the sidebar's session list can
 * live in memory and survive "create a chat" / "switch sessions" / "start a new one"
 * without needing to be re-fetched from the server or reset to a stale prop on every click
 * — the actual fix for the "shouldn't need to reload" gap (see ChatSessionNavList /
 * ChatWorkspace for the consuming side).
 *
 * Originally scoped to `app/chat/layout.tsx` (fetched only on `/chat/*`). Hoisted to the
 * root layout so `NavRail`'s merged "Chat" nav item — which renders on every page, not
 * just `/chat/*` — can reach the same live list instead of NavRail growing a second,
 * competing data source. See `NavRail`'s and the root layout's own docstrings for the
 * full rationale and the fetch-cost tradeoff that hoist implies.
 */
export function ChatSessionsProvider({
  initialSessions,
  children,
}: {
  initialSessions: ChatSession[];
  children: ReactNode;
}) {
  const [sessions, setSessions] = useState<ChatSession[]>(initialSessions);
  const [dirty, setDirty] = useState(false);
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(() => new Set());

  const setSessionBusy = useCallback((id: string, busy: boolean) => {
    setBusyIds((prev) => {
      if (prev.has(id) === busy) return prev;
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  // While any listed session is working server-side (e.g. one the user navigated away from, or one
  // started in another tab), refresh just the `turnInProgress` flags every few seconds so its sidebar
  // badge clears by itself. Stops as soon as nothing is running; never touches titles/order.
  const anyServerBusy = sessions.some((s) => s.turnInProgress);
  useEffect(() => {
    if (!anyServerBusy) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const fresh = await listChatSessions();
        if (stopped) return;
        const busyNow = new Map(fresh.map((f) => [f.id, !!f.turnInProgress]));
        setSessions((prev) => {
          let changed = false;
          const next = prev.map((s) => {
            const b = busyNow.get(s.id);
            if (b === undefined || b === !!s.turnInProgress) return s;
            changed = true;
            return { ...s, turnInProgress: b };
          });
          return changed ? next : prev;
        });
      } catch {
        // transient — try again on the next tick
      }
      if (!stopped) timer = setTimeout(tick, 4000);
    };
    timer = setTimeout(tick, 4000);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [anyServerBusy]);

  const upsertSessionTitle = useCallback<ChatSessionsContextValue["upsertSessionTitle"]>((id, title, fallback) => {
    if (!title) return;
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.id === id);
      if (idx === -1) {
        const now = new Date().toISOString();
        const fresh: ChatSession = {
          id,
          userId: fallback.userId,
          title,
          status: "active",
          contextSummary: "",
          targetScorecardId: fallback.targetScorecardId,
          createdAt: now,
          lastActivityAt: now,
        };
        return [fresh, ...prev];
      }
      if (prev[idx].title === title) return prev; // no visible change — skip the re-render
      const next = [...prev];
      next[idx] = { ...next[idx], title, lastActivityAt: new Date().toISOString() };
      return next;
    });
  }, []);

  const removeSession = useCallback((id: string) => {
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, []);

  const value = useMemo<ChatSessionsContextValue>(
    () => ({ sessions, upsertSessionTitle, removeSession, dirty, setDirty, busyIds, setSessionBusy }),
    [sessions, upsertSessionTitle, removeSession, dirty, busyIds, setSessionBusy],
  );

  return <ChatSessionsContext.Provider value={value}>{children}</ChatSessionsContext.Provider>;
}

export function useChatSessions(): ChatSessionsContextValue {
  const ctx = useContext(ChatSessionsContext);
  if (!ctx) throw new Error("useChatSessions must be used within a ChatSessionsProvider (see the root app/layout.tsx)");
  return ctx;
}
