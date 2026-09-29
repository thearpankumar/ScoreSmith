import Link from "next/link";
import { Headphones, GitPullRequest, FilePlus2 } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";

const TILES = [
  {
    href: "/chat/new?prompt=" + encodeURIComponent("Build a scorecard to grade customer support email replies."),
    icon: Headphones,
    title: "Customer support quality",
    description: "Grade support replies for accuracy, tone, and policy compliance.",
  },
  {
    href: "/chat/new?prompt=" + encodeURIComponent("Build a lightweight code review quality gate."),
    icon: GitPullRequest,
    title: "Code review gate",
    description: "A fast pre-merge signal covering correctness and readability.",
  },
  {
    href: "/chat/new",
    icon: FilePlus2,
    title: "Start from scratch",
    description: "Describe any type of work — we'll propose the KPIs.",
  },
] as const;

export function QuickStartTiles() {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      {TILES.map((tile) => {
        const Icon = tile.icon;
        return (
          <Link key={tile.title} href={tile.href}>
            <GlassCard
              elevation={1}
              className="h-full p-4 transition-transform hover:-translate-y-0.5 hover:shadow-[var(--shadow-2)]"
            >
              <Icon className="mb-2 size-5 text-lemon-ink" aria-hidden />
              <p className="text-sm font-semibold text-ink">{tile.title}</p>
              <p className="mt-1 text-xs text-ink-muted">{tile.description}</p>
            </GlassCard>
          </Link>
        );
      })}
    </div>
  );
}
