"use client";

import { useState } from "react";
import Link from "next/link";
import { BarChart3, Settings, Users } from "lucide-react";

import { NotificationBell } from "@/components/notifications/NotificationBell";
import { AccountDialog } from "./AccountDialog";
import { initials, type SessionUser } from "./UserMenu";

/**
 * The app bar above every signed-in page. From `md` up it is only the notification bell, floating at the top right with
 * no chrome and no reserved height (pages put their own heading on that row). Below `md`, where the
 * left rail is replaced by the bottom bar, it is a glass bar with the brand, the account button and, for administrators, the Users /
 * Settings shortcuts (the bottom bar only carries Chat / Charts / Evaluations).
 */
export function TopBar({ user }: { user: SessionUser | null }) {
  const [accountOpen, setAccountOpen] = useState(false);
  const isAdmin = user?.role === "admin";
  return (
    <header className="glass-subtle sticky top-4 z-30 mb-3 flex min-h-14 items-center justify-between gap-2 rounded-2xl px-3 py-1.5 md:absolute md:right-0 md:top-[7px] md:mb-0 md:min-h-0 md:border-transparent! md:bg-transparent! md:p-0 md:shadow-none! md:backdrop-filter-none!">
      <div className="flex min-w-0 items-center gap-2">
        <span className="flex min-w-0 items-center gap-2 md:hidden">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-lemon text-lemon-ink" aria-hidden>
            <BarChart3 className="size-4.5" />
          </span>
          <span className="truncate text-sm font-semibold text-ink">Quality Scorecards</span>
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {isAdmin && (
          <span className="flex items-center gap-1.5 md:hidden">
            <Link
              href="/admin/users"
              aria-label="Users"
              className="glass-tint glass-interactive flex size-11 items-center justify-center rounded-full text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
            >
              <Users className="size-5" aria-hidden />
            </Link>
            <Link
              href="/settings"
              aria-label="Settings"
              className="glass-tint glass-interactive flex size-11 items-center justify-center rounded-full text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
            >
              <Settings className="size-5" aria-hidden />
            </Link>
          </span>
        )}
        <NotificationBell />
        {user && (
          <>
            <button
              type="button"
              onClick={() => setAccountOpen(true)}
              aria-label={`Your account (${user.name})`}
              className="flex size-11 items-center justify-center rounded-full bg-ink/90 text-xs font-semibold text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)] md:hidden"
            >
              {initials(user.name, user.email)}
            </button>
            <AccountDialog user={user} open={accountOpen} onOpenChange={setAccountOpen} />
          </>
        )}
      </div>
    </header>
  );
}
