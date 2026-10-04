"use client";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { PIPELINE_STEPS } from "@/lib/eval-status";
import type { KpiNode } from "@/lib/types";

/** Side panel: what happens to a submission (stepper) and the leaf KPIs that will be scored. */
export function KpiPanel({ leaves }: { leaves: KpiNode[] }) {
  return (
    <SolidPanel className="flex min-w-0 flex-col gap-5 p-5 lg:sticky lg:top-4">
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">What happens</p>
        <ol className="mt-3 flex items-start" aria-label="Pipeline steps">
          {PIPELINE_STEPS.map((step, i) => (
            <li key={step} className="relative flex min-w-0 flex-1 flex-col items-center gap-1.5 text-center">
              {i > 0 && <span className="absolute right-1/2 top-3 h-px w-full bg-hairline" aria-hidden />}
              <span className="relative z-10 flex size-6 items-center justify-center rounded-full bg-lemon text-[11px] font-semibold text-lemon-ink">
                {i + 1}
              </span>
              <span className="text-[11px] font-medium text-ink">{step}</span>
            </li>
          ))}
        </ol>
        <p className="mt-3 text-xs text-ink-muted">
          Files are fetched, text and images are extracted, video is transcribed, then the AI judge scores every KPI
          and names the evaluation.
        </p>
      </div>

      <div className="min-h-0">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">KPIs the judge will score</p>
          <Badge variant="muted" className="tabular-nums">
            {leaves.length}
          </Badge>
        </div>
        <ul className="thin-scrollbar mt-3 max-h-[24rem] space-y-1.5 overflow-y-auto pr-1">
          {leaves.map((kpi, i) => (
            <li key={kpi.id} className="flex items-start gap-2 text-sm text-ink">
              <span
                className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border border-hairline text-[9px] tabular-nums text-ink-muted"
                aria-hidden
              >
                {i + 1}
              </span>
              <span className="min-w-0 break-words">{kpi.name}</span>
            </li>
          ))}
        </ul>
      </div>
    </SolidPanel>
  );
}
