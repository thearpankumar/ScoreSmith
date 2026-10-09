import type { ReactNode } from "react";

import { AppShell } from "@/components/layout/AppShell";
import { LiveStatusProvider } from "@/components/live/LiveStatusProvider";
import { ChatSessionsProvider } from "@/components/chat/ChatSessionsContext";
import { getCurrentUser, listChatSessions } from "@/lib/api-client";

// Forced dynamic: every signed-in route does its own live, per-user backend fetch per request
// (see e.g. app/(app)/charts/page.tsx), and this layout itself fetches the current user's chat
// sessions with the request's session cookie, so nothing here can be prerendered.
export const dynamic = "force-dynamic";

/**
 * `ChatSessionsProvider` used to live in `app/chat/layout.tsx`, scoped to `/chat/*` only
 * (see that provider's own docstring for the original "fetch once, keep live across
 * navigation" rationale — unchanged). It's hoisted up to the signed-in layout now that
 * `NavRail`'s merged "Chat" nav item needs the same live session list — see `NavRail`'s
 * docstring for the full reasoning. This does mean every signed-in page makes one extra
 * lightweight backend call (listing the current user's chat sessions) even on pages that
 * never show them (Charts/Evaluations/Settings) — a real, if small, cost, accepted in
 * exchange for a single real data source instead of a second competing one. `NavRail`
 * itself still only *renders* the list on `/chat/*` routes, so the fetch's only "waste"
 * is the network call, not any extra UI. Failure is swallowed (falls back to an empty
 * list) so a chat-sessions hiccup can no longer take down every page in the app.
 * A 401 is not swallowed: `apiFetch` turns it into a redirect to /login.
 */
async function loadInitialChatSessions() {
  try {
    return await listChatSessions();
  } catch (err) {
    if (isNextControlFlow(err)) throw err; // a redirect to /login must propagate
    return [];
  }
}

function isNextControlFlow(err: unknown): boolean {
  const digest = err && typeof err === "object" && "digest" in err ? (err as { digest?: unknown }).digest : undefined;
  return typeof digest === "string" && (digest.startsWith("NEXT_REDIRECT") || digest.startsWith("NEXT_NOT_FOUND"));
}

async function loadSessionUser() {
  try {
    const me = await getCurrentUser();
    return { name: me.name, email: me.email, role: me.role, username: me.username ?? null };
  } catch (err) {
    if (isNextControlFlow(err)) throw err;
    return null;
  }
}

export default async function AppLayout({ children }: { children: ReactNode }) {
  const [sessions, user] = await Promise.all([loadInitialChatSessions(), loadSessionUser()]);
  return (
    <ChatSessionsProvider initialSessions={sessions}>
      <LiveStatusProvider>
        <AppShell user={user}>{children}</AppShell>
      </LiveStatusProvider>
    </ChatSessionsProvider>
  );
}
