import Link from "next/link";
import { ChevronRight } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { RagBadge } from "@/components/design-system/RagBadge";
import { Badge } from "@/components/ui/badge";
import { listEvaluations } from "@/lib/api-client";
import { formatDateTime } from "@/lib/utils";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function EvaluationsPage() {
  const evaluations = await listEvaluations();
  const sorted = [...evaluations].sort((a, b) => b.submittedAt.localeCompare(a.submittedAt));

  return (
    <div className="flex flex-col gap-4 py-2">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Evaluations</h1>
        <p className="mt-1 text-sm text-ink-muted">All evaluations across every scorecard, most recent first.</p>
      </div>

      {sorted.length === 0 ? (
        <SolidPanel className="p-6 text-sm text-ink-muted">No evaluations yet.</SolidPanel>
      ) : (
        <SolidPanel className="divide-y divide-hairline">
          {sorted.map((evaluation) => (
            <Link
              key={evaluation.id}
              href={`/charts/${evaluation.scorecardId}/evaluations/${evaluation.id}`}
              className="flex items-center justify-between gap-3 px-5 py-4 transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--focus)]"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="truncate text-sm font-medium text-ink">{evaluation.name}</p>
                  <Badge variant="muted">{evaluation.domain}</Badge>
                </div>
                <p className="mt-0.5 truncate text-xs text-ink-muted" suppressHydrationWarning>
                  {evaluation.scorecardName} · {evaluation.evaluatedByName} · {formatDateTime(evaluation.submittedAt)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                {/* An evaluation that never finished scoring has no real score — don't show it as "0.0 Critical". */}
                {evaluation.status === "completed" ? (
                  <RagBadge score={evaluation.finalWeightedScore} size="sm" />
                ) : (
                  <Badge variant="muted">
                    {evaluation.status === "failed" ? "Not scored" : evaluation.status === "pending" ? "Pending" : "In progress"}
                  </Badge>
                )}
                <ChevronRight className="size-4 text-ink-muted" aria-hidden />
              </div>
            </Link>
          ))}
        </SolidPanel>
      )}
    </div>
  );
}
