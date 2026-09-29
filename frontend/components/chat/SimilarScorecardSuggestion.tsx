"use client";

import { Sparkles } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { SimilarScorecardSuggestion as Suggestion } from "@/lib/types";

export function SimilarScorecardSuggestion({
  suggestion,
  onChoose,
  disabled,
}: {
  suggestion: Suggestion;
  onChoose: (action: "use" | "adapt" | "fresh") => void;
  disabled?: boolean;
}) {
  return (
    <GlassCard elevation={2} className="ml-9 max-w-[85%] p-4">
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
        <Sparkles className="size-3.5 text-lemon-ink" aria-hidden />
        Found a similar scorecard
      </div>
      <div className="mb-1 flex items-center gap-2">
        <p className="text-sm font-semibold text-ink">{suggestion.scorecardName}</p>
        <Badge variant="muted">{suggestion.domain}</Badge>
        <Badge variant="soft">{Math.round(suggestion.similarity * 100)}% match</Badge>
      </div>
      <p className="mb-3 text-sm text-ink-muted">{suggestion.summary}</p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => onChoose("use")} disabled={disabled}>
          Use as-is
        </Button>
        <Button size="sm" variant="outline" onClick={() => onChoose("adapt")} disabled={disabled}>
          Adapt it
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChoose("fresh")} disabled={disabled}>
          Start fresh
        </Button>
      </div>
    </GlassCard>
  );
}
