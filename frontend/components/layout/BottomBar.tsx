"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";
import { NAV_ITEMS } from "./nav-items";
import { SignOutTab } from "./UserMenu";

/**
 * Mobile bottom bar (replaces `NavRail`, which is `md:`-and-up only). Glass chrome per
 * the plan's nav styling rule.
 *
 * Chat-session access on mobile: `NavRail`'s merged "Chat" nav item now nests the full
 * session list underneath it (see `NavRail`'s docstring) — that doesn't fit a bottom bar,
 * so it's not attempted here. The "Chat" icon below is still a plain link to `/chat` (as
 * every item here always was); `app/chat/page.tsx` keeps its own compact, capped-height
 * session list ONLY below the `md` breakpoint (i.e. exactly while this bar is the one in
 * use) as the mobile entry point for browsing/switching past chats — that's the existing
 * pattern this bar already relied on, preserved as-is rather than reimplemented here.
 */
export function BottomBar() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Primary"
      className="glass-strong fixed inset-x-3 bottom-3 z-40 flex items-center justify-around rounded-2xl px-1 py-1.5 md:hidden"
    >
      {NAV_ITEMS.map((item) => {
        const active = pathname.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex flex-1 flex-col items-center gap-0.5 rounded-xl px-1 py-1.5 text-[11px] font-medium transition-colors",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
              active ? "bg-lemon text-lemon-ink font-semibold" : "text-ink-muted",
            )}
          >
            <Icon className="size-4.5" aria-hidden />
            {item.label}
          </Link>
        );
      })}
      <SignOutTab />
    </nav>
  );
}
