import { ChatSessionsProvider } from "@/components/chat/ChatSessionsContext";
import { listChatSessions } from "@/lib/api-client";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

/**
 * Shared layout for every `/chat/*` route. Next.js does NOT remount a layout when
 * navigating between routes it wraps (only the page segment underneath does) — that's
 * exactly what makes this the right place for `ChatSessionsProvider`'s live session-list
 * state to live: fetched here ONCE per real entry into `/chat/*`, then kept current
 * entirely client-side (via SessionList's hover-delete and ChatWorkspace's title
 * updates) across every "new chat" / "switch session" navigation underneath, instead of
 * being re-fetched from scratch (and visibly reset/reloaded) on every single click. See
 * ChatSessionsContext's docstring for the full rationale.
 */
export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  const sessions = await listChatSessions();
  return <ChatSessionsProvider initialSessions={sessions}>{children}</ChatSessionsProvider>;
}
