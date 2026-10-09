import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";

import "./marketing.css";

// Same self-hosted Inter as the sign-in pages (no request to Google at runtime).
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-inter",
  display: "swap",
});

export const viewport: Viewport = { width: "device-width", initialScale: 1, viewportFit: "cover", themeColor: "#fffaf0" };

export const metadata: Metadata = {
  title: { absolute: "Score Smith: Design Better KPIs. Evaluate Smarter." },
  description:
    "A modern platform to design, track and evaluate KPIs that align teams, improve performance and turn data into meaningful outcomes.",
  alternates: { canonical: "/" },
  openGraph: {
    type: "website",
    siteName: "Score Smith",
    title: "Score Smith: Design Better KPIs. Evaluate Smarter.",
    description: "Design, track and evaluate KPIs that align teams and drive real progress.",
  },
  twitter: { card: "summary", title: "Score Smith", description: "Design Better KPIs. Evaluate Smarter." },
};

/**
 * Public marketing pages: no app shell, no chat-sessions provider, no authenticated backend call. The cream/gold look is
 * scoped to `.mk` (marketing.css) so nothing leaks into the signed-in app.
 */
export default function MarketingLayout({ children }: { children: ReactNode }) {
  return <div className={`mk ${inter.variable}`}>{children}</div>;
}
