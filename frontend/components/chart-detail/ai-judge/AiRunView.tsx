"use client";

import { useState } from "react";
import Link from "next/link";
import { CheckCircle2, Layers, Plus } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { RagBadge } from "@/components/design-system/RagBadge";
import { EvaluationProgress } from "@/components/evaluation-progress/EvaluationProgress";
import { EvaluationStatusBadge } from "@/components/evaluations/EvaluationStatusBadge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { getAiBatch, getEvaluation } from "@/lib/api-client";
import { useAdaptivePoll } from "@/lib/useAdaptivePoll";
import type { AiBatch, Evaluation, EvaluationProgress as ProgressSnapshot } from "@/lib/types";

/**
 * What the Evaluate tab shows in place of the input form once a submission is queued, so the user
 * watches it happen right here instead of landing on a separate page.
 */

/** One submission: the live pipeline view, with a result summary above it once it completes. */
export function SingleRun({ evaluation, onNew }: { evaluation: Evaluation; onNew: () => void }) {
  const [result, setResult] = useState<Evaluation | null>(null);
  const [finished, setFinished] = useState(false);

  async function handleFinished(final: ProgressSnapshot) {
    if (final.status !== "completed") return;
    setFinished(true);
    try {
      const full = await getEvaluation(evaluation.id);
      if (full) setResult(full);
    } catch {
      // The summary is a nicety: the "View full result" link below works without it.
    }
  }

  const resultHref = `/charts/${evaluation.scorecardId}/evaluations/${evaluation.id}`;

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
      {finished && (
        <GlassCard elevation={2} className="flex flex-col gap-4 p-5 sm:p-7" role="status">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-3">
              <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-[var(--rag-excellent)] text-white">
                <CheckCircle2 className="size-5" aria-hidden />
              </span>
              <div className="min-w-0">
                <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Evaluation complete</p>
                <h2 className="break-words text-lg font-semibold text-ink sm:text-xl">
                  {result?.name ?? evaluation.name}
                </h2>
              </div>
            </div>
            {result && <RagBadge score={result.finalWeightedScore} />}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button asChild>
              <Link href={resultHref}>View full result</Link>
            </Button>
            <Button type="button" variant="outline" onClick={onNew}>
              <Plus aria-hidden />
              Evaluate another
            </Button>
          </div>
        </GlassCard>
      )}
      <EvaluationProgress embedded evaluation={evaluation} onFinished={handleFinished} onNew={onNew} />
    </div>
  );
}

/** Several submissions: overall progress and one live row per evaluation. */
export function BatchRun({
  batchId,
  initial,
  onNew,
}: {
  batchId: string;
  initial: Evaluation[];
  onNew: () => void;
}) {
  const [batch, setBatch] = useState<AiBatch | null>(null);

  const rows = batch?.evaluations ?? initial;
  const total = batch?.total ?? initial.length;
  const counts = batch?.counts ?? { queued: initial.length, running: 0, completed: 0, failed: 0 };
  const remaining = counts.queued + counts.running;
  const finished = counts.completed + counts.failed;

  useAdaptivePoll(
    async (signal) => {
      const next = await getAiBatch(batchId, signal);
      if (next) setBatch(next);
    },
    remaining > 0 || batch === null,
    1.5,
  );

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
      <GlassCard elevation={2} className="flex flex-col gap-4 p-5 sm:p-7" aria-label="Batch progress">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-full bg-lemon text-lemon-ink">
              <Layers className="size-5" aria-hidden />
            </span>
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">AI evaluation batch</p>
              <h2 className="text-lg font-semibold text-ink sm:text-xl">
                {remaining === 0 ? "Batch finished" : "Batch in progress"}
                <span className="ml-2 text-sm font-normal tabular-nums text-ink-muted">
                  {finished}/{total} done
                </span>
              </h2>
            </div>
          </div>
          <p className="text-xs text-ink-muted tabular-nums">
            {counts.queued} queued · {counts.running} running · {counts.completed} completed
            {counts.failed > 0 ? ` · ${counts.failed} failed` : ""}
          </p>
        </div>
        <Progress label="Batch progress" value={finished} max={Math.max(total, 1)} />
        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="outline">
            <Link href={`/evaluations?batch=${batchId}`}>Open in evaluations</Link>
          </Button>
          <Button type="button" variant="ghost" onClick={onNew}>
            <Plus aria-hidden />
            Evaluate more
          </Button>
        </div>
      </GlassCard>

      <GlassCard elevation={1} className="p-2 sm:p-3">
        <ul className="divide-y divide-hairline" aria-label="Evaluations in this batch">
          {rows.map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-3 px-3 py-2.5">
              <div className="min-w-0">
                <Link
                  href={`/charts/${e.scorecardId}/evaluations/${e.id}`}
                  className="block truncate text-sm font-medium text-ink hover:underline"
                  title={e.name}
                >
                  {e.name}
                </Link>
                {e.subjectEmail && <p className="truncate text-xs text-ink-muted">{e.subjectEmail}</p>}
              </div>
              <EvaluationStatusBadge evaluation={e} />
            </li>
          ))}
        </ul>
      </GlassCard>
    </div>
  );
}
