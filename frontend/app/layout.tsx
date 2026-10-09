import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "Quality Scorecard System",
  description: "Build, reuse, and apply quality scorecards — a generic scorecard creation & rating tool.",
};

/**
 * The root layout only owns `<html>` / `<body>`. The signed-in app chrome (nav rail, chat
 * sessions provider, live backend fetch) lives in `app/(app)/layout.tsx`, and the public
 * auth pages (login / signup / forgot / reset) in `app/(auth)/layout.tsx` — so a signed-out
 * visitor never triggers an authenticated backend call just by loading `/login`.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
