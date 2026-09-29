import Link from "next/link";
import { Target, User } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Badge } from "@/components/ui/badge";
import { ScorecardStatusBadge } from "./ScorecardStatusBadge";
import type { Scorecard } from "@/lib/types";

export function ScorecardCard({ scorecard }: { scorecard: Scorecard }) {
  return (
    <Link href={`/charts/${scorecard.id}`} className="block h-full">
      <GlassCard
        elevation={1}
        className="flex h-full flex-col gap-3 p-4 transition-transform hover:-translate-y-0.5 hover:shadow-[var(--shadow-2)]"
      >
        <div className="flex items-start justify-between gap-2">
          <Badge variant="muted">{scorecard.domain}</Badge>
          <ScorecardStatusBadge status={scorecard.status} />
        </div>
        <div>
          <p className="text-sm font-semibold leading-snug text-ink">{scorecard.name}</p>
          <p className="mt-1 line-clamp-2 text-xs text-ink-muted">{scorecard.purposeStatement}</p>
        </div>
        <div className="mt-auto flex items-center justify-between pt-2 text-xs text-ink-muted">
          <span className="flex items-center gap-1">
            <User className="size-3" aria-hidden />
            {scorecard.ownerName}
          </span>
          <span className="flex items-center gap-1">
            <Target className="size-3" aria-hidden />
            Target {scorecard.targetScore.toFixed(1)}
          </span>
        </div>
        <p className="text-[11px] text-ink-muted">{scorecard.kpiCount} KPIs</p>
      </GlassCard>
    </Link>
  );
}
