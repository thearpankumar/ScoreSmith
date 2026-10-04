import { BatchProgress } from "@/components/evaluations/BatchProgress";
import { EvaluationsListClient } from "@/components/evaluations/EvaluationsListClient";
import { getAiBatch, listEvaluations } from "@/lib/api-client";
import { ALL, parseStatusFilter } from "@/lib/eval-filters";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function EvaluationsPage({
  searchParams,
}: {
  searchParams: Promise<{ batch?: string | string[]; scorecard?: string | string[]; status?: string | string[] }>;
}) {
  const sp = await searchParams;
  const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
  const batchId = first(sp.batch);
  const initialFilters = { scorecardId: first(sp.scorecard) || ALL, status: parseStatusFilter(first(sp.status)) };

  const evaluations = await listEvaluations();
  const sorted = [...evaluations].sort((a, b) => b.submittedAt.localeCompare(a.submittedAt));
  const batch = batchId
    ? await getAiBatch(batchId).catch(() => undefined)
    : undefined;

  return (
    <div className="flex flex-col gap-4 py-2">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Evaluations</h1>
        <p className="mt-1 text-sm text-ink-muted">
          {batchId ? "Progress of your batch, most recent first." : "All evaluations across every scorecard, most recent first."}
        </p>
      </div>

      {batchId && <BatchProgress batchId={batchId} initial={batch ?? null} />}

      <EvaluationsListClient evaluations={sorted} batchId={batchId} initialFilters={initialFilters} />
    </div>
  );
}
