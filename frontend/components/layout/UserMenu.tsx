"use client";

import { useState } from "react";
import { LogOut } from "lucide-react";

import { logout } from "@/lib/auth-client";
import { cn } from "@/lib/utils";

export interface SessionUser {
  name: string;
  email: string;
}

function initials(name: string, email: string): string {
  const parts = (name || email).trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] ?? "?").slice(0, 2);
  return letters.toUpperCase();
}

/** Ends the session on the server (revokes the refresh token, clears the cookies), then goes to the homepage. */
async function signOut(): Promise<void> {
  // The server clears the cookies on success, so plain "/" shows the homepage. If the request failed the cookies may
  // still look valid: "?expired=1" makes the middleware clear them instead of bouncing the visitor back into /chat.
  let target = "/";
  try {
    await logout();
  } catch {
    target = "/?expired=1";
  }
  // A full navigation also drops every cached RSC payload.
  window.location.assign(target);
}

/** Desktop rail footer: who is signed in, and Sign out. */
export function UserMenu({ user, collapsed }: { user: SessionUser | null; collapsed: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className={cn("glass-tint mt-2 flex shrink-0 items-center gap-2 rounded-xl p-2", collapsed && "flex-col")}>
      {user && (
        <span
          className="flex size-8 shrink-0 items-center justify-center rounded-full bg-ink/90 text-xs font-semibold text-white"
          title={`${user.name} <${user.email}>`}
          aria-hidden
        >
          {initials(user.name, user.email)}
        </span>
      )}
      {user && !collapsed && (
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-sm font-medium text-ink">{user.name}</span>
          <span className="block truncate text-xs text-ink-muted">{user.email}</span>
        </span>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          void signOut();
        }}
        title="Sign out"
        aria-label="Sign out"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-ink-muted transition-colors hover:bg-white/70 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)] disabled:opacity-50"
      >
        <LogOut className="size-4.5" aria-hidden />
      </button>
    </div>
  );
}

/** Compact variant for the mobile bottom bar. */
export function SignOutTab() {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        void signOut();
      }}
      className="flex flex-1 flex-col items-center gap-0.5 rounded-xl px-1 py-1.5 text-[11px] font-medium text-ink-muted transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)] disabled:opacity-50"
    >
      <LogOut className="size-4.5" aria-hidden />
      Sign out
    </button>
  );
}
