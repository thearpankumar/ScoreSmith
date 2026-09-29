import type { ReactNode } from "react";

import { NavRail } from "./NavRail";
import { BottomBar } from "./BottomBar";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto flex w-full max-w-[1400px] gap-4 p-4">
      <NavRail />
      <main className="min-w-0 flex-1 pb-20 md:pb-4">{children}</main>
      <BottomBar />
    </div>
  );
}
