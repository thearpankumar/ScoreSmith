"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";

import { cn } from "@/lib/utils";
import { usePersistedState } from "@/lib/usePersistedState";
import { NAV_ITEMS } from "./nav-items";

const STORAGE_KEY = "qs.navRailCollapsed";

/**
 * Desktop left rail. Glass chrome per the plan's nav styling rule.
 *
 * Fixed-height (`h-[calc(100vh-2rem)]`) but its content is only ~200px tall — the rest
 * was dead space with no way to reclaim it. Now collapsible to a narrow icon-only rail;
 * the state is a per-viewer UI preference (not app data), so it's persisted to
 * localStorage rather than the backend, and restored on the next page load.
 */
export function NavRail() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = usePersistedState(STORAGE_KEY, false);

  return (
    <nav
      aria-label="Primary"
      className={cn(
        "glass-elev-1 sticky top-4 hidden h-[calc(100vh-2rem)] shrink-0 flex-col gap-1 rounded-2xl p-4 transition-[width] duration-150 md:flex",
        collapsed ? "w-[4.5rem] items-center px-2" : "w-60",
      )}
    >
      <div className={cn("mb-4 flex items-center gap-2", collapsed ? "justify-center px-0" : "px-2")}>
        <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-lemon text-sm font-bold text-lemon-ink">
          QS
        </span>
        {!collapsed && <span className="text-sm font-semibold text-ink">Quality Scorecards</span>}
      </div>

      {NAV_ITEMS.map((item) => {
        const active = pathname.startsWith(item.href);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            title={collapsed ? item.label : undefined}
            className={cn(
              "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
              collapsed && "w-full justify-center px-0",
              active ? "bg-lemon text-lemon-ink font-semibold shadow-sm" : "text-ink-muted hover:bg-black/5 hover:text-ink",
            )}
          >
            <Icon className="size-4.5 shrink-0" aria-hidden />
            <span className={cn(collapsed && "sr-only")}>{item.label}</span>
          </Link>
        );
      })}

      <div className="mt-auto flex justify-center pt-2">
        <button
          type="button"
          onClick={() => setCollapsed(!collapsed)}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          title={collapsed ? "Expand navigation" : "Collapse navigation"}
          className="rounded-full p-1.5 text-ink-muted hover:bg-black/5 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
        >
          {collapsed ? <PanelLeftOpen className="size-4.5" aria-hidden /> : <PanelLeftClose className="size-4.5" aria-hidden />}
        </button>
      </div>
    </nav>
  );
}
