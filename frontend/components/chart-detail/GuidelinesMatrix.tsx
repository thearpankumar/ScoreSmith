"use client";

import { useMemo, useState } from "react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { leafKpiNodes } from "@/lib/kpi-tree";
import { cn, formatDate } from "@/lib/utils";
import type { Evaluation, KpiNode } from "@/lib/types";

const LEVELS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

/**
 * KPI x 11-score-level guideline matrix. Mandatory SOLID surface (dense
 * data, per the design system rule — never glass here). Sticky header row
 * and sticky first column so long matrices stay readable while scrolling;
 * the column matching a chosen evaluation's actual score per KPI is
 * highlighted with --lemon-soft.
 */
export function GuidelinesMatrix({ kpiNodes, evaluations }: { kpiNodes: KpiNode[]; evaluations: Evaluation[] }) {
  const [highlightEvalId, setHighlightEvalId] = useState<string>(evaluations[0]?.id ?? "none");
  const rows = useMemo(() => leafKpiNodes(kpiNodes), [kpiNodes]);
  // Human-readable ancestry ("Code Quality > Correctness") instead of the raw ltree path,
  // which for real data is a chain of UUID hex labels.
  const breadcrumbById = useMemo(() => {
    const byId = new Map(kpiNodes.map((n) => [n.id, n]));
    const out = new Map<string, string>();
    kpiNodes.forEach((n) => {
      const names: string[] = [];
      let cur = n.parentId ? byId.get(n.parentId) : undefined;
      while (cur && names.length < 10) {
        names.unshift(cur.name);
        cur = cur.parentId ? byId.get(cur.parentId) : undefined;
      }
      out.set(n.id, names.join(" › "));
    });
    return out;
  }, [kpiNodes]);

  const highlightEval = evaluations.find((e) => e.id === highlightEvalId);
  const highlightScoreByKpi = useMemo(() => {
    const map = new Map<string, number>();
    highlightEval?.kpiResults.forEach((r) => map.set(r.kpiNodeId, r.score));
    return map;
  }, [highlightEval]);

  return (
    <div className="flex flex-col gap-3">
      {evaluations.length > 0 && (
        <div className="flex items-center gap-2 text-xs font-medium text-ink-muted">
          <label htmlFor="highlight-eval">Highlight scores from</label>
          <select
            id="highlight-eval"
            value={highlightEvalId}
            onChange={(e) => setHighlightEvalId(e.target.value)}
            className="h-9 rounded-lg border border-hairline bg-solid px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
          >
            <option value="none">None</option>
            {evaluations.map((ev) => (
              <option key={ev.id} value={ev.id} suppressHydrationWarning>
                {ev.name} — {formatDate(ev.submittedAt)}
              </option>
            ))}
          </select>
        </div>
      )}

      <SolidPanel className="max-h-[70vh] overflow-auto thin-scrollbar">
        {/* border-separate (not collapse) so sticky header/first-column cells
            position reliably across browsers. */}
        <table className="w-full border-separate border-spacing-0 text-sm">
          <thead>
            <tr>
              <th className="sticky left-0 top-0 z-20 min-w-56 border-b border-r border-hairline bg-solid px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-muted">
                KPI
              </th>
              {LEVELS.map((level) => (
                <th
                  key={level}
                  className="sticky top-0 z-10 min-w-44 border-b border-hairline bg-solid px-3 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-muted"
                >
                  Level {level}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((kpi) => {
              const highlightedLevel = highlightScoreByKpi.get(kpi.id);
              return (
                <tr key={kpi.id}>
                  <td className="sticky left-0 z-10 border-b border-r border-hairline bg-solid px-4 py-3 align-top">
                    <p className="font-medium text-ink">{kpi.name}</p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      L{kpi.level} · {kpi.weight}%{breadcrumbById.get(kpi.id) ? ` · ${breadcrumbById.get(kpi.id)}` : ""}
                    </p>
                  </td>
                  {LEVELS.map((level) => {
                    const guideline = kpi.guidelines?.find((g) => g.scoreLevel === level);
                    const isHighlighted = highlightedLevel === level;
                    return (
                      <td
                        key={level}
                        className={cn(
                          "border-b border-hairline px-3 py-3 align-top text-xs leading-relaxed text-ink-muted",
                          isHighlighted && "bg-lemon-soft font-medium text-ink",
                        )}
                      >
                        {guideline ? (
                          <>
                            <span>{guideline.qualitativeText}</span>
                            {guideline.quantitativeCriteria && (
                              <span className="mt-1 block font-mono text-[11px] text-ink">
                                {guideline.quantitativeCriteria}
                              </span>
                            )}
                          </>
                        ) : (
                          <span className="italic text-[var(--rag-poor)]">Not defined</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </SolidPanel>
    </div>
  );
}
