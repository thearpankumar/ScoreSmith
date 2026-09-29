import { GlassCard } from "@/components/design-system/GlassCard";
import { ChatSessionNavList } from "@/components/chat/ChatSessionNavList";
import { PromptBox } from "@/components/home/PromptBox";
import { QuickStartTiles } from "@/components/home/QuickStartTiles";
import { MOCK_CURRENT_USER } from "@/lib/mock-data";

// This page itself does no fetching (see the docstring below) — the root `app/layout.tsx`
// already forces the whole app dynamic (`dynamic = "force-dynamic"`), which per Next.js's
// route-segment-config rules covers this page too.

/**
 * Chat is the app's landing page ("/" redirects here). The main column is the "describe
 * what you want to rate" prompt that starts a new scorecard-building chat.
 *
 * Past sessions: on desktop/tablet (`md` and up) they now live in `NavRail`'s merged
 * "Chat" section (see that component's docstring) instead of a separate column here, so
 * this page renders nothing extra there. Below `md`, `NavRail` itself is hidden
 * (`BottomBar` is the mobile nav instead, and a full nested session list doesn't fit a
 * bottom bar — see `BottomBar`'s docstring), so this page keeps its own compact,
 * capped-height session list ONLY at that breakpoint — the one existing mobile entry
 * point for browsing/switching past chats, preserved as-is rather than dropped.
 *
 * Either way, the list itself is fed by `ChatSessionsContext` (see the root
 * `app/layout.tsx`), not a prop fetched here — it's already loaded (and kept live) at the
 * shared root-layout level, so this page doesn't need its own copy.
 */
export default function ChatIndexPage() {
  return (
    <div className="flex flex-col gap-4">
      <div className="md:hidden">
        <GlassCard elevation={1} className="max-h-64 overflow-y-auto thin-scrollbar p-3">
          <ChatSessionNavList activeSessionId="" />
        </GlassCard>
      </div>
      <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-1 flex-col gap-6 py-2">
        <div>
          <p className="text-sm text-ink-muted">Welcome back, {MOCK_CURRENT_USER.name.split(" ")[0]}</p>
          <h1 className="mt-1 text-2xl font-semibold text-ink">What do you want to rate today?</h1>
        </div>
        <PromptBox />
        <section aria-labelledby="quick-start-heading">
          <h2 id="quick-start-heading" className="mb-3 text-sm font-semibold text-ink-muted">
            Quick start
          </h2>
          <QuickStartTiles />
        </section>
      </div>
    </div>
  );
}
