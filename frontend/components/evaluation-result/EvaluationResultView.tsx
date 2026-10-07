import Link from "next/link";
import { Calendar, TriangleAlert, User } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { RagBadge } from "@/components/design-system/RagBadge";
import { BulletChart } from "./BulletChart";
import { KpiSummaryTable } from "./KpiSummaryTable";
import { KpiTreeTable } from "./KpiTreeTable";
import { effectiveTarget } from "@/lib/rag";
import { formatDateTime } from "@/lib/utils";
import type { Evaluation, KpiNode } from "@/lib/types";

export function EvaluationResultView({ evaluation, kpiNodes }: { evaluation: Evaluation; kpiNodes: KpiNode[] }) {
  // A scorecard without a target is judged against the app default (7), same as the colours.
  const target = effectiveTarget(evaluation.targetScore);
  const gap = Math.round((target - evaluation.finalWeightedScore) * 100) / 100;
  const metTarget = gap <= 0;
  // The real backend judge run is a Bedrock call (see backend/app/ai/judge.py) — in an
  // environment with no AWS credentials it fails and the evaluation is persisted with
  // status "failed" and no kpiResults, rather than a real score. Show that plainly
  // instead of rendering a misleading all-zero scorecard.
  const notScored = evaluation.status !== "completed";

  return (
    <div className="flex flex-col gap-6 py-2">
      <div>
        <Link href={`/charts/${evaluation.scorecardId}`} className="text-xs font-medium text-ink-muted hover:text-ink">
          {evaluation.scorecardName}
        </Link>
        <h1 className="mt-0.5 text-2xl font-semibold text-ink">{evaluation.name}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-ink-muted">
          <span className="flex items-center gap-1">
            <User className="size-3" aria-hidden />
            {evaluation.evaluatedByName}
          </span>
          <span className="flex items-center gap-1" suppressHydrationWarning>
            <Calendar className="size-3" aria-hidden />
            {formatDateTime(evaluation.submittedAt)}
          </span>
          <span>{evaluation.inputSummary}</span>
        </div>
      </div>

      {notScored ? (
        <GlassCard elevation={1} className="flex items-start gap-3 p-5">
          <TriangleAlert className="mt-0.5 size-5 shrink-0 text-[var(--rag-poor)]" aria-hidden />
          <div>
            <p className="text-sm font-semibold text-ink">
              {evaluation.status === "failed" ? "This evaluation could not be scored" : "This evaluation hasn’t finished scoring"}
            </p>
            <p className="mt-1 text-sm text-ink-muted">
              {evaluation.judgeError ??
                "The AI judge is unavailable right now, so no score or KPI breakdown was produced. Run it again once the AI service is reachable."}
            </p>
          </div>
        </GlassCard>
      ) : (
        <>
          <GlassCard elevation={2} className="p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Final weighted score</p>
                <div className="mt-1 flex items-baseline gap-2">
                  <span className="text-3xl font-semibold text-ink">{evaluation.finalWeightedScore.toFixed(1)}</span>
                  <span className="text-sm text-ink-muted">/ 10 · target {target.toFixed(1)}</span>
                </div>
                <p className={metTarget ? "mt-1 text-sm text-[var(--rag-excellent)]" : "mt-1 text-sm text-[var(--rag-poor)]"}>
                  {metTarget ? `Meets target by ${Math.abs(gap).toFixed(1)}` : `${gap.toFixed(1)} below target`}
                </p>
              </div>
              <RagBadge score={evaluation.finalWeightedScore} target={target} />
            </div>
            <div className="mt-4">
              <BulletChart score={evaluation.finalWeightedScore} target={target} />
            </div>
          </GlassCard>

          <section>
            <h2 className="mb-3 text-sm font-semibold text-ink">KPI summary</h2>
            <p className="mb-3 text-xs text-ink-muted">Sorted by gap to target — the KPIs needing the most attention first.</p>
            <KpiSummaryTable results={evaluation.kpiResults} targetScore={target} />
          </section>

          <section>
            <h2 className="mb-3 text-sm font-semibold text-ink">KPI hierarchy &amp; reasoning</h2>
            <p className="mb-3 text-xs text-ink-muted">Expand a parent to see its sub-KPIs. Open “View” for the judge&apos;s reasoning and cited evidence.</p>
            <KpiTreeTable kpiNodes={kpiNodes} results={evaluation.kpiResults} target={target} />
          </section>
        </>
      )}
    </div>
  );
}
