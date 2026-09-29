import { EvaluationsListClient } from "@/components/evaluations/EvaluationsListClient";
import { listEvaluations } from "@/lib/api-client";

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

      <EvaluationsListClient evaluations={sorted} />
    </div>
  );
}
