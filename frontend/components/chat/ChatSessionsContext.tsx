"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

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
    () => ({ sessions, upsertSessionTitle, removeSession, dirty, setDirty }),
    [sessions, upsertSessionTitle, removeSession, dirty],
  );

  return <ChatSessionsContext.Provider value={value}>{children}</ChatSessionsContext.Provider>;
}

export function useChatSessions(): ChatSessionsContextValue {
  const ctx = useContext(ChatSessionsContext);
  if (!ctx) throw new Error("useChatSessions must be used within a ChatSessionsProvider (see the root app/layout.tsx)");
  return ctx;
}
