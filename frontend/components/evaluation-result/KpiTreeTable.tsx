"use client";

import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  useReactTable,
  type ExpandedState,
} from "@tanstack/react-table";
import { ChevronRight, MessageSquareText } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { RagBadge } from "@/components/design-system/RagBadge";
import { ReasoningDrawer } from "./ReasoningDrawer";
import { buildKpiTree, computeKpiRollup, type NestedKpiNode } from "@/lib/kpi-tree";
import { cn } from "@/lib/utils";
import type { EvaluationKpiResult, KpiNode } from "@/lib/types";

const columnHelper = createColumnHelper<NestedKpiNode>();

/**
 * Expandable Level1->Level4 TREE TABLE (per the plan, deliberately not a
 * node graph — business users read tables). Parent rows show a
 * weight-rolled-up score computed from their children; leaf rows show the
 * judge's actual score and open a reasoning drawer.
 */
export function KpiTreeTable({
  kpiNodes,
  results,
  target,
}: {
  kpiNodes: KpiNode[];
  results: EvaluationKpiResult[];
  /** The scorecard target the score badges are coloured against. */
  target?: number | null;
}) {
  const [expanded, setExpanded] = useState<ExpandedState>(true);
  const [drawerResult, setDrawerResult] = useState<EvaluationKpiResult | null>(null);

  const resultsByKpiId = useMemo(() => new Map(results.map((r) => [r.kpiNodeId, r])), [results]);

  const { tree, scoreByNodeId } = useMemo(() => {
    const leafScores: Record<string, number> = {};
    results.forEach((r) => (leafScores[r.kpiNodeId] = r.score));
    const rollup = computeKpiRollup(kpiNodes, leafScores);
    return { tree: buildKpiTree(kpiNodes), scoreByNodeId: rollup.scoreByNodeId };
  }, [kpiNodes, results]);

  const columns = useMemo(
    () => [
      columnHelper.accessor("name", {
        header: "KPI",
        cell: ({ row, getValue }) => (
          <div className="flex items-center gap-1.5" style={{ paddingLeft: `${row.depth * 1.25}rem` }}>
            {row.getCanExpand() ? (
              <button
                type="button"
                onClick={row.getToggleExpandedHandler()}
                className="flex size-5 shrink-0 items-center justify-center rounded text-ink-muted hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
                aria-label={row.getIsExpanded() ? "Collapse" : "Expand"}
              >
                <ChevronRight className={cn("size-3.5 transition-transform", row.getIsExpanded() && "rotate-90")} />
              </button>
            ) : (
              <span className="size-5 shrink-0" />
            )}
            <span className={cn("text-sm", row.depth === 0 ? "font-semibold text-ink" : "text-ink")}>
              {getValue()}
            </span>
          </div>
        ),
      }),
      columnHelper.accessor("level", {
        header: "Level",
        cell: (info) => <Badge variant="muted">L{info.getValue()}</Badge>,
      }),
      columnHelper.display({
        id: "score",
        header: "Score",
        cell: ({ row }) => {
          const score = scoreByNodeId[row.original.id];
          return score != null ? <RagBadge score={score} size="sm" target={target} /> : <span className="text-ink-muted">—</span>;
        },
      }),
      columnHelper.accessor("weight", {
        header: "Weight",
        cell: (info) => <span className="tabular-nums text-ink-muted">{info.getValue()}%</span>,
      }),
      columnHelper.display({
        id: "reasoning",
        header: "Reasoning",
        cell: ({ row }) => {
          const result = resultsByKpiId.get(row.original.id);
          if (!result) return <span className="text-xs text-ink-muted">—</span>;
          return (
            <button
              type="button"
              onClick={() => setDrawerResult(result)}
              className="flex items-center gap-1 rounded-full border border-hairline px-2.5 py-1 text-xs font-medium text-ink transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
            >
              <MessageSquareText className="size-3" aria-hidden />
              View
            </button>
          );
        },
      }),
    ],
    [resultsByKpiId, scoreByNodeId, target],
  );

  const table = useReactTable({
    data: tree,
    columns,
    state: { expanded },
    onExpandedChange: setExpanded,
    getSubRows: (row) => row.children,
    getCoreRowModel: getCoreRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
  });

  return (
    <>
      <SolidPanel className="overflow-x-auto thin-scrollbar">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id} className="border-b border-hairline bg-bg/60">
                {headerGroup.headers.map((header) => (
                  <th key={header.id} className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-muted">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-b border-hairline last:border-0 hover:bg-bg/40">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-2.5">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </SolidPanel>
      <ReasoningDrawer
        result={drawerResult}
        open={!!drawerResult}
        onOpenChange={(open) => !open && setDrawerResult(null)}
        target={target}
      />
    </>
  );
}
