import type { Metadata } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";

import { AuthScene } from "@/components/auth/AuthScene";

import "./auth.css";

// The sign-in design is set entirely in Inter (weights 400-700), matched against the mock by cap height and
// glyph widths. Self-hosted by next/font at build time: no request to Google when the page loads.
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Sign in · KPI Metrics",
  description: "Sign in to design, track and evaluate quality scorecards.",
  robots: { index: false, follow: false },
};

/**
 * Public, signed-out pages. The cream/gold glass theme is scoped to the `.auth-theme` wrapper (its own CSS variables in
 * auth.css): nothing here touches the light tokens the signed-in app uses.
 */
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className={`auth-theme ${inter.variable}`}>
      <AuthScene>{children}</AuthScene>
    </div>
  );
}
