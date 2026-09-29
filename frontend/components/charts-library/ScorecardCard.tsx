import Link from "next/link";
import { Target, Trash2, User } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Badge } from "@/components/ui/badge";
import { ScorecardStatusBadge } from "./ScorecardStatusBadge";
import { cn } from "@/lib/utils";
import type { Scorecard } from "@/lib/types";

export function ScorecardCard({
  scorecard,
  onRequestDelete,
}: {
  scorecard: Scorecard;
  /** Opens the confirm-delete dialog (owned by ChartsLibraryClient, which also holds the
   * list state the removal needs to update) — see that component. */
  onRequestDelete: (scorecard: Scorecard) => void;
}) {
  return (
    // `group relative` wraps the card's Link + its hover-reveal delete button as SIBLINGS,
    // not delete-button-inside-Link — nesting a <button> inside the <a> a Link renders
    // would be invalid, click-ambiguous HTML.
    <div className="group relative h-full">
      <Link href={`/charts/${scorecard.id}`} className="block h-full">
        <GlassCard
          elevation={1}
          className="flex h-full flex-col gap-3 p-4 transition-transform hover:-translate-y-0.5 hover:shadow-[var(--shadow-2)]"
        >
          <div className="flex items-start justify-between gap-2 pr-6">
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
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onRequestDelete(scorecard);
        }}
        aria-label={`Delete “${scorecard.name}”`}
        title="Delete scorecard"
        className={cn(
          "absolute right-3 top-3 rounded-full bg-solid/80 p-1.5 text-ink-muted shadow-sm backdrop-blur-sm",
          "opacity-0 transition-opacity hover:bg-[var(--rag-poor)]/10 hover:text-[var(--rag-poor)]",
          // Hover-reveal, but visible on keyboard focus too (group-focus-within /
          // focus-visible — hover-only is unreachable without a mouse) and always visible
          // on touch/coarse-pointer devices (no hover concept there).
          "group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100",
          "[@media(hover:none)]:opacity-100",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        )}
      >
        <Trash2 className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
