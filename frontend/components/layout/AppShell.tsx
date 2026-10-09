import type { ReactNode } from "react";

import { NavRail } from "./NavRail";
import { BottomBar } from "./BottomBar";
import { TopBar } from "./TopBar";
import type { SessionUser } from "./UserMenu";

export function AppShell({ children, user = null }: { children: ReactNode; user?: SessionUser | null }) {
  return (
    <>
      {/* Static yellow light behind everything so the glass surfaces have something to blur. */}
      <div className="app-backdrop" aria-hidden="true" />
      <div className="relative z-10 mx-auto flex w-full max-w-[1400px] gap-4 p-4">
        <NavRail user={user} />
        <main className="relative min-w-0 flex-1 pb-20 md:pb-4">
          <TopBar user={user} />
          {children}
        </main>
        <BottomBar />
      </div>
    </>
  );
}
