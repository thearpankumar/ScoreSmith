"use client";

import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { getRagBand } from "@/lib/rag";
import { cn } from "@/lib/utils";
import type { EvaluationKpiResult } from "@/lib/types";

interface Row extends EvaluationKpiResult {
  contribution: number;
  gapToTarget: number;
}

const columnHelper = createColumnHelper<Row>();

export function KpiSummaryTable({
  results,
  targetScore,
}: {
  results: EvaluationKpiResult[];
  targetScore: number;
}) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "gapToTarget", desc: true }]);

  const data = useMemo<Row[]>(
    () =>
      results.map((r) => ({
        ...r,
        contribution: (r.score * r.weight) / 100,
        gapToTarget: Math.round((targetScore - r.score) * 100) / 100,
      })),
    [results, targetScore],
  );

  const columns = useMemo(
    () => [
      columnHelper.accessor("kpiName", {
        header: "KPI",
        cell: (info) => <span className="font-medium text-ink">{info.getValue()}</span>,
      }),
      columnHelper.accessor("score", {
        header: "Score",
        cell: (info) => {
          const score = info.getValue();
          const band = getRagBand(score);
          return (
            <div className="flex items-center gap-2">
              <div className="h-2 w-24 overflow-hidden rounded-full bg-black/5">
                <div
                  className="h-full rounded-full"
                  style={{ width: `${(score / 10) * 100}%`, backgroundColor: band.color }}
                />
              </div>
              <span className="tabular-nums text-ink-muted">{score.toFixed(1)}</span>
            </div>
          );
        },
      }),
      columnHelper.accessor("weight", {
        header: "Weight",
        cell: (info) => <span className="tabular-nums text-ink-muted">{info.getValue()}%</span>,
      }),
      columnHelper.accessor("contribution", {
        header: "Weighted contribution",
        cell: (info) => <span className="tabular-nums text-ink-muted">{info.getValue().toFixed(2)}</span>,
      }),
      columnHelper.accessor("gapToTarget", {
        header: "Gap to target",
        cell: (info) => {
          const gap = info.getValue();
          return (
            <span className={cn("tabular-nums font-medium", gap > 0 ? "text-[var(--rag-poor)]" : "text-[var(--rag-excellent)]")}>
              {gap > 0 ? `-${gap.toFixed(1)}` : `+${Math.abs(gap).toFixed(1)}`}
            </span>
          );
        },
      }),
    ],
    [],
  );

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <SolidPanel className="overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id} className="border-b border-hairline bg-bg/60">
              {headerGroup.headers.map((header) => {
                const sortDir = header.column.getIsSorted();
                return (
                  <th key={header.id} className="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-muted">
                    {header.column.getCanSort() ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className="flex items-center gap-1 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sortDir === "asc" && <ArrowUp className="size-3" aria-hidden />}
                        {sortDir === "desc" && <ArrowDown className="size-3" aria-hidden />}
                        {!sortDir && <ArrowUpDown className="size-3 opacity-40" aria-hidden />}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                );
              })}
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
  );
}
