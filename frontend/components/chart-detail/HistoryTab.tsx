import Link from "next/link";
import { ChevronRight } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { RagBadge } from "@/components/design-system/RagBadge";
import { Badge } from "@/components/ui/badge";
import { formatDateTime } from "@/lib/utils";
import type { Evaluation, EvaluationStatus } from "@/lib/types";

const UNSCORED_LABEL: Record<Exclude<EvaluationStatus, "completed">, string> = {
  failed: "Not scored",
  pending: "Pending",
  in_progress: "In progress",
};

/**
 * Every evaluation of this scorecard, across ALL of its versions. When the scorecard has
 * more than one version (e.g. after "Refine with assistant"), each row says which
 * version it was scored against, since older rows used a different KPI structure.
 * Evaluations that never finished scoring show their status instead of a misleading
 * "0.0 / Critical" band.
 */
export function HistoryTab({
  scorecardId,
  evaluations,
  versionNumbers = {},
  currentVersionId,
}: {
  scorecardId: string;
  evaluations: Evaluation[];
  versionNumbers?: Record<string, number>;
  currentVersionId?: string;
}) {
  if (evaluations.length === 0) {
    return (
      <SolidPanel className="p-6 text-sm text-ink-muted">
        No evaluations yet — run one from the Evaluate tab.
      </SolidPanel>
    );
  }

  const multiVersion = Object.keys(versionNumbers).length > 1;
  const sorted = [...evaluations].sort((a, b) => b.submittedAt.localeCompare(a.submittedAt));

  return (
    <SolidPanel className="divide-y divide-hairline">
      {sorted.map((evaluation) => {
        const versionNumber = versionNumbers[evaluation.scorecardVersionId];
        const isCurrent = evaluation.scorecardVersionId === currentVersionId;
        return (
          <Link
            key={evaluation.id}
            href={`/charts/${scorecardId}/evaluations/${evaluation.id}`}
            className="flex items-center justify-between gap-3 px-5 py-4 transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--focus)]"
          >
            <div className="min-w-0">
              <p className="flex flex-wrap items-center gap-2 truncate text-sm font-medium text-ink">
                {evaluation.name}
                {multiVersion && versionNumber !== undefined && (
                  <Badge
                    variant={isCurrent ? "outline" : "muted"}
                    title={isCurrent ? "Scored against the current version" : "Scored against an earlier version"}
                  >
                    v{versionNumber}
                    {isCurrent ? " · current" : ""}
                  </Badge>
                )}
              </p>
              <p className="mt-0.5 text-xs text-ink-muted" suppressHydrationWarning>
                {evaluation.evaluatedByName} · {formatDateTime(evaluation.submittedAt)}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-3">
              {evaluation.status === "completed" ? (
                <RagBadge score={evaluation.finalWeightedScore} size="sm" />
              ) : (
                <Badge variant="muted">{UNSCORED_LABEL[evaluation.status]}</Badge>
              )}
              <ChevronRight className="size-4 text-ink-muted" aria-hidden />
            </div>
          </Link>
        );
      })}
    </SolidPanel>
  );
}
