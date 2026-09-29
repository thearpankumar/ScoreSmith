import { SessionList } from "@/components/chat/SessionList";
import { PromptBox } from "@/components/home/PromptBox";
import { QuickStartTiles } from "@/components/home/QuickStartTiles";
import { MOCK_CURRENT_USER } from "@/lib/mock-data";

// This page itself does no fetching (see the docstring below) — `app/chat/layout.tsx`'s
// own `dynamic = "force-dynamic"` already forces the whole /chat/* route dynamic, which
// per Next.js's route-segment-config rules covers this page too.

/**
 * Chat is the app's landing page ("/" redirects here). Past sessions sit on the left
 * (stacked on top on narrow screens); the main column is the "describe what you want
 * to rate" prompt that starts a new scorecard-building chat.
 *
 * The session list itself is fed by ChatSessionsContext (see app/chat/layout.tsx), not a
 * prop fetched here — the list is already loaded (and kept live) at the shared /chat/*
 * layout level, so this page doesn't need its own copy.
 */
export default function ChatIndexPage() {
  return (
    <div className="flex flex-col gap-4 md:flex-row">
      <div className="md:w-64 md:shrink-0">
        <div className="max-h-64 overflow-y-auto thin-scrollbar md:sticky md:top-4 md:max-h-[calc(100vh-2rem)]">
          <SessionList activeSessionId="" />
        </div>
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
