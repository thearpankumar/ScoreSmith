"use client";

import { useState } from "react";
import Link from "next/link";
import { Share2, Target, Trash2, User, Users } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { ShareDialog } from "@/components/sharing/ShareDialog";
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
  const [shareOpen, setShareOpen] = useState(false);
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
          <div className="flex items-start justify-between gap-2 pr-9">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant="muted">{scorecard.domain}</Badge>
              {scorecard.isShared && (
                <Badge variant="soft" title={scorecard.myRole === "editor" ? `Shared with you by ${scorecard.ownerName}` : "Shared with collaborators"}>
                  <Users className="size-3" aria-hidden />
                  {scorecard.myRole === "editor" ? "Shared with you" : "Shared"}
                </Badge>
              )}
            </div>
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
          <p className="min-h-6 pr-10 text-[11px] text-ink-muted">{scorecard.kpiCount} KPIs</p>
        </GlassCard>
      </Link>
      {/* Share: owner or accepted editor can invite (the dialog adapts to the role). Sibling of the Link, like delete. */}
      <button
        type="button"
        onClick={() => setShareOpen(true)}
        aria-label={`Share “${scorecard.name}”`}
        title="Share"
        className="absolute bottom-1.5 right-1.5 flex size-9 items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-lemon/60 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
      >
        <Share2 className="size-4" aria-hidden />
      </button>
      <ShareDialog
        open={shareOpen}
        onOpenChange={setShareOpen}
        scorecardId={scorecard.id}
        scorecardName={scorecard.name}
        myRole={scorecard.myRole ?? "owner"}
      />
      {/* Delete: the owner moves the chart to the trash; a collaborator only removes their own access (the dialog
          in ChartsLibraryClient says which). */}
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onRequestDelete(scorecard);
        }}
        aria-label={scorecard.myRole === "editor" ? `Remove “${scorecard.name}” from my charts` : `Move “${scorecard.name}” to the trash`}
        title={scorecard.myRole === "editor" ? "Remove from my charts" : "Move to trash"}
        className={cn(
          "absolute right-1.5 top-1.5 flex size-11 items-center justify-center rounded-full text-ink-muted",
          "opacity-0 transition-opacity hover:bg-[var(--rag-poor)]/10 hover:text-[var(--rag-poor)]",
          // Hover-reveal, but visible on keyboard focus too (group-focus-within /
          // focus-visible — hover-only is unreachable without a mouse) and always visible
          // on touch/coarse-pointer devices (no hover concept there).
          "group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100",
          "[@media(hover:none)]:opacity-100",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        )}
      >
        <Trash2 className="size-4" aria-hidden />
      </button>
    </div>
  );
}
