"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { PanelLeftClose, PanelLeftOpen, Plus } from "lucide-react";

import { cn } from "@/lib/utils";
import { usePersistedState } from "@/lib/usePersistedState";
import { useChatSessions } from "@/components/chat/ChatSessionsContext";
import { ChatSessionNavList } from "@/components/chat/ChatSessionNavList";
import { NAV_ITEMS } from "./nav-items";

const STORAGE_KEY = "qs.navRailCollapsed";

/**
 * Desktop/tablet left rail. Glass chrome per the plan's nav styling rule.
 *
 * Fixed-height (`h-[calc(100vh-2rem)]`). Vertical order: logo -> "+ New scorecard" ->
 * the chat session list (only populated/expanded while on a `/chat*` route — see the
 * merged-sidebar note below) -> all four nav tabs together (Chat, Charts, Evaluations,
 * Settings) -> collapse toggle pinned to the bottom via `mt-auto`. The session list sits
 * ABOVE the nav tabs rather than nested directly under the Chat tab so that Chat stays
 * visually grouped with the other three tabs as one cohesive block, instead of an
 * open-ended scrollable list wedging Chat apart from Charts/Evaluations/Settings. The
 * surrounding group used to be vertically centered (`justify-center`) back when it was a
 * handful of fixed-height rows with nothing else below — that no longer works now that an
 * open-ended, independently-scrollable session list can sit in the middle of it, so the
 * group is top-aligned instead and the session-list slot alone is what grows/scrolls
 * (`flex-1 min-h-0 overflow-y-auto`), while everything else (logo, button, nav items,
 * collapse toggle) keeps its natural height. Collapsible to a narrow icon-only rail; the
 * state is a per-viewer UI preference (not app data), so it's persisted to localStorage
 * rather than the backend, and restored on the next page load.
 *
 * --- Merged sidebar (chat session list lives here now) ---
 * This used to be a plain nav with no data dependency. There used to be a SEPARATE
 * `SessionList` column rendered only on the Chat tab, fed by `ChatSessionsContext` (a
 * provider scoped to `/chat/*`, see `app/chat/layout.tsx`'s old docstring). Per the
 * owner's request, that column is now merged into this rail, nested under the "Chat" nav
 * item. The catch: `NavRail` renders on EVERY page (inside the root `AppShell`), outside
 * where that `/chat/*`-scoped provider used to live — a React context only flows to
 * descendants, and this rail is a sibling of the routed page content, not a descendant of
 * it, so it structurally can't reach a provider scoped under `/chat/*`.
 *
 * Resolution: `ChatSessionsProvider` was hoisted from `app/chat/layout.tsx` up to the
 * root `app/layout.tsx` (wrapping `AppShell`, so both this rail and every page can reach
 * it) — see that file's docstring for the fetch-cost tradeoff this implies and why it was
 * accepted. This rail then does its own, separate gating: the nested list is only
 * rendered (and only takes up layout space) when `pathname` is actually under `/chat` —
 * on every other page (Charts/Evaluations/Settings) the Chat item renders as a plain link
 * with nothing nested beneath it, exactly as before this change. That keeps a single real
 * data source (`ChatSessionsContext`) instead of a second, competing one, while avoiding
 * chat-list clutter on unrelated pages.
 */
export function NavRail() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = usePersistedState(STORAGE_KEY, false);
  const { sessions } = useChatSessions();

  const onChatRoute = pathname === "/chat" || pathname.startsWith("/chat/");
  // "/chat/abc123" -> "abc123"; "/chat", "/chat/new"'s own page resolves "new" itself, so
  // passing "new" through here (no real session to highlight yet) is harmless.
  const activeSessionId = onChatRoute ? (pathname.match(/^\/chat\/([^/]+)/)?.[1] ?? "") : "";

  return (
    <nav
      aria-label="Primary"
      className={cn(
        "glass-elev-1 sticky top-4 hidden h-[calc(100vh-2rem)] shrink-0 flex-col gap-1 rounded-2xl p-4 transition-[width] duration-150 md:flex",
        collapsed ? "w-[4.5rem] items-center px-2" : "w-60",
      )}
    >
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-hidden">
        <div className={cn("mb-4 flex shrink-0 items-center gap-2", collapsed ? "justify-center px-0" : "px-2")}>
          <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-lemon text-sm font-bold text-lemon-ink">
            QS
          </span>
          {!collapsed && <span className="text-sm font-semibold text-ink">Quality Scorecards</span>}
        </div>

        <Link
          href="/chat/new"
          title={collapsed ? "New scorecard" : undefined}
          className={cn(
            "mb-3 flex shrink-0 items-center gap-2 rounded-full bg-lemon px-4 py-2.5 text-sm font-semibold text-lemon-ink shadow-sm transition-colors hover:bg-lemon-hover",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
            collapsed ? "size-10 justify-center px-0 py-0" : "justify-center",
          )}
        >
          <Plus className="size-4.5 shrink-0" aria-hidden />
          <span className={cn(collapsed && "sr-only")}>New scorecard</span>
        </Link>

        {/* Spacer/chat-session-list slot: ALWAYS rendered (not just on a /chat* route) so
            the nav-tabs group below is pinned to a consistent bottom-ish position on every
            page — if this were only present on /chat, the nav group would sit right under
            "New scorecard" on Charts/Evaluations/Settings and visibly jump position when
            navigating back to a chat page with sessions. Sits ABOVE the nav tabs (not
            nested under the Chat tab specifically) so Chat stays grouped with
            Charts/Evaluations/Settings below rather than being wedged apart from them by
            an open-ended scrollable list. The session list itself only renders as content
            on a /chat* route; elsewhere this is just empty flexible space. When collapsed,
            the rail can't show full session cards (no room, same as every other item
            losing its label) — dropped entirely rather than squeezed, in keeping with the
            collapsed rail's existing icon+tooltip-only pattern; the Chat item's tooltip
            gains a session count instead, so that information isn't just lost. */}
        <div className="mb-1 min-h-0 flex-1 overflow-y-auto thin-scrollbar">
          {onChatRoute && !collapsed && <ChatSessionNavList activeSessionId={activeSessionId} />}
        </div>

        <div className="flex shrink-0 flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <NavItemLink
              key={item.href}
              item={item}
              pathname={pathname}
              collapsed={collapsed}
              sessionCount={item.href === "/chat" ? sessions.length : undefined}
              onChatRoute={item.href === "/chat" ? onChatRoute : undefined}
            />
          ))}
        </div>
      </div>

      {/* Collapse toggle: straddles the rail's right edge at mid-height, so it reads as a
          handle on the sidebar's border instead of a row at the bottom. */}
      <button
        type="button"
        onClick={() => setCollapsed(!collapsed)}
        aria-expanded={!collapsed}
        aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
        title={collapsed ? "Expand navigation" : "Collapse navigation"}
        className="absolute top-1/2 -right-3.5 z-10 flex size-7 -translate-y-1/2 items-center justify-center rounded-full border border-hairline bg-solid text-ink-muted shadow-sm transition-colors hover:bg-lemon hover:text-lemon-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
      >
        {collapsed ? <PanelLeftOpen className="size-4" aria-hidden /> : <PanelLeftClose className="size-4" aria-hidden />}
      </button>
    </nav>
  );
}

function NavItemLink({
  item,
  pathname,
  collapsed,
  sessionCount,
  onChatRoute,
}: {
  item: (typeof NAV_ITEMS)[number];
  pathname: string;
  collapsed: boolean;
  /** Chat item only: folded into its collapsed tooltip so the session count isn't just
   * silently lost when the nested list can't be shown (see the collapsed-rail note above). */
  sessionCount?: number;
  onChatRoute?: boolean;
}) {
  const active = pathname.startsWith(item.href);
  const Icon = item.icon;
  const tooltip =
    collapsed && onChatRoute && sessionCount
      ? `${item.label} (${sessionCount})`
      : collapsed
        ? item.label
        : undefined;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      title={tooltip}
      className={cn(
        "flex shrink-0 items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        collapsed && "w-full justify-center px-0",
        active ? "bg-lemon text-lemon-ink font-semibold shadow-sm" : "text-ink-muted hover:bg-black/5 hover:text-ink",
      )}
    >
      <Icon className="size-4.5 shrink-0" aria-hidden />
      <span className={cn(collapsed && "sr-only")}>{item.label}</span>
    </Link>
  );
}
