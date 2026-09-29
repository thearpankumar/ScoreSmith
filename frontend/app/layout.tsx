import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AppShell } from "@/components/layout/AppShell";
import { ChatSessionsProvider } from "@/components/chat/ChatSessionsContext";
import { listChatSessions } from "@/lib/api-client";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quality Scorecard System",
  description: "Build, reuse, and apply quality scorecards — a generic scorecard creation & rating tool.",
};

// Forced dynamic: every route already opted into this individually (see e.g.
// app/charts/page.tsx, app/settings/page.tsx — each does its own live backend fetch per
// request); making it explicit here at the root just matches what was already true, and
// is required now that the root layout itself does a live fetch (see below) rather than
// only the /chat/* subtree.
export const dynamic = "force-dynamic";

/**
 * `ChatSessionsProvider` used to live in `app/chat/layout.tsx`, scoped to `/chat/*` only
 * (see that provider's own docstring for the original "fetch once, keep live across
 * navigation" rationale — unchanged). It's hoisted up to here now that `NavRail`'s merged
 * "Chat" nav item needs the same live session list — see `NavRail`'s docstring for the
 * full reasoning. This does mean every page now makes one extra lightweight backend call
 * (listing the current user's chat sessions) even on pages that never show them
 * (Charts/Evaluations/Settings) — a real, if small, cost, accepted in exchange for a
 * single real data source instead of a second competing one. `NavRail` itself still only
 * *renders* the list on `/chat/*` routes, so the fetch's only "waste" is the network call,
 * not any extra UI. Failure is swallowed (falls back to an empty list) so a chat-sessions
 * hiccup can no longer take down every page in the app — it used to only affect `/chat/*`.
 */
async function loadInitialChatSessions() {
  try {
    return await listChatSessions();
  } catch {
    return [];
  }
}

export default async function RootLayout({ children }: { children: ReactNode }) {
  const sessions = await loadInitialChatSessions();
  return (
    <html lang="en">
      <body>
        <ChatSessionsProvider initialSessions={sessions}>
          <AppShell>{children}</AppShell>
        </ChatSessionsProvider>
      </body>
    </html>
  );
}
