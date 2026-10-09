import { BatchProgress } from "@/components/evaluations/BatchProgress";
import { EvaluationsListClient } from "@/components/evaluations/EvaluationsListClient";
import { getAiBatch } from "@/lib/api-client";
import { DEFAULT_EVAL_QUERY, fetchEvaluationsPage, type EvalPage } from "@/lib/collab-client";
import { parseStatusFilter } from "@/lib/eval-filters";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function EvaluationsPage({
  searchParams,
}: {
  searchParams: Promise<{ batch?: string | string[]; scorecard?: string | string[]; status?: string | string[]; q?: string | string[] }>;
}) {
  const sp = await searchParams;
  const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
  const batchId = first(sp.batch);
  const initialQuery = {
    ...DEFAULT_EVAL_QUERY,
    scorecardId: first(sp.scorecard) || null,
    status: parseStatusFilter(first(sp.status)),
    batchId: batchId || null,
    q: first(sp.q) ?? "",
  };

  // The first page is rendered on the server (no empty flash); every later page streams in on scroll.
  let initialPage: EvalPage | null = null;
  try {
    initialPage = await fetchEvaluationsPage(initialQuery, { limit: 40 });
  } catch (err) {
    if (err && typeof err === "object" && "digest" in err) throw err; // Next.js control flow (redirect to /login)
    initialPage = null; // the client retries and shows its own error state
  }
  const batch = batchId ? await getAiBatch(batchId).catch(() => undefined) : undefined;

  return (
    <div className="flex flex-col gap-4 py-2">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Evaluations</h1>
        <p className="mt-1 text-sm text-ink-muted">
          {batchId
            ? "Progress of your batch, most recent first."
            : "Every evaluation on the charts you own or share, newest first. Filter, search and sort; the list loads as you scroll."}
        </p>
      </div>

      {batchId && <BatchProgress batchId={batchId} initial={batch ?? null} />}

      <EvaluationsListClient initialPage={initialPage} initialQuery={initialQuery} />
    </div>
  );
}
