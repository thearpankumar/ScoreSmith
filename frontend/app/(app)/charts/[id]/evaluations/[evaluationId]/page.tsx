import { EvaluationResultView } from "@/components/evaluation-result/EvaluationResultView";
import { EvaluationResultClientLoader } from "@/components/evaluation-result/EvaluationResultClientLoader";
import { EvaluationProgress } from "@/components/evaluation-progress/EvaluationProgress";
import { getEvaluation, getScorecardWithVersion } from "@/lib/api-client";
import { showsProgressView } from "@/lib/eval-status";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function EvaluationResultPage({
  params,
}: {
  params: Promise<{ id: string; evaluationId: string }>;
}) {
  const { id, evaluationId } = await params;

  const evaluation = await getEvaluation(evaluationId);
  if (evaluation) {
    // Queued / running AI evaluations (and failed ones with an error_code) get the live progress
    // view; it calls router.refresh() on completion so this branch then renders the result.
    if (showsProgressView(evaluation)) {
      return <EvaluationProgress evaluation={{ ...evaluation, scorecardId: evaluation.scorecardId || id }} />;
    }
    const withVersion = await getScorecardWithVersion(evaluation.scorecardId);
    return <EvaluationResultView evaluation={evaluation} kpiNodes={withVersion?.version.kpiNodes ?? []} />;
  }

  // Not in the static mock dataset — might be a freshly created mock
  // evaluation living only in this browser tab's sessionStorage.
  return <EvaluationResultClientLoader scorecardId={id} evaluationId={evaluationId} />;
}
