"use client";

import { AlertTriangle, CheckCircle2, Download, Loader2, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { MAX_EXPORT } from "@/lib/eval-selection";

export type ExportStatus =
  | { kind: "idle" }
  | { kind: "exporting" }
  | { kind: "deleting"; done: number; total: number }
  | { kind: "success"; message: string }
  // `action: "delete"` errors have no Retry (the failed rows stay selected; press Delete again).
  | { kind: "error"; message: string; action?: "export" | "delete" };

/**
 * Sticky bulk-action bar for the Evaluations list: how many rows are selected (and how many of those are hidden by
 * the current filters), the include-reasoning option, and the Export to Excel action with its progress / result
 * states. Selection is announced in text and via the checked checkboxes — never by colour alone.
 */
export function EvaluationSelectionBar({
  total,
  exportable,
  hidden,
  allMatching = false,
  maxExport = MAX_EXPORT,
  includeReasoning,
  onIncludeReasoning,
  status,
  onExport,
  onDelete,
  onClear,
  onDeselectHidden,
}: {
  total: number;
  /** How many of the selected rows can go into the export (completed with a score). */
  exportable: number;
  hidden: number;
  /** True for the server-side "all N matching this filter" selection. */
  allMatching?: boolean;
  /** The export cap that applies to this selection (200 by ids, 500 by filter). */
  maxExport?: number;
  includeReasoning: boolean;
  onIncludeReasoning: (value: boolean) => void;
  status: ExportStatus;
  onExport: () => void;
  onDelete: () => void;
  onClear: () => void;
  onDeselectHidden: () => void;
}) {
  const exporting = status.kind === "exporting";
  const deleting = status.kind === "deleting";
  const busy = exporting || deleting;
  const overCap = !allMatching && exportable > maxExport;
  if (total === 0 && status.kind !== "success") return null;

  return (
    <div
      role="region"
      aria-label="Bulk actions"
      aria-busy={busy}
      // Below `md` the fixed bottom nav (z-40, ~4.5rem tall) would sit on top of this bar, so lift it clear of it.
      className="sticky bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-30 mx-auto mt-4 flex w-full max-w-3xl flex-col gap-2 rounded-2xl border border-hairline bg-ink px-4 py-3 text-sm text-bg shadow-lg md:bottom-4"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <p className="min-w-0 font-medium tabular-nums" aria-live="polite">
          {total.toLocaleString()} selected{allMatching ? " (all matching the filter)" : ""}
          {hidden > 0 && (
            <span className="font-normal opacity-80">
              {" "}
              ({hidden} not in view{" "}
              <button type="button" onClick={onDeselectHidden} className="underline underline-offset-2 hover:opacity-100">
                Deselect hidden
              </button>
              )
            </span>
          )}
        </p>
        <label className="flex cursor-pointer items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={includeReasoning}
            onChange={(e) => onIncludeReasoning(e.target.checked)}
            disabled={busy}
            className="size-3.5 accent-[var(--lemon)]"
          />
          Include reasoning &amp; evidence
        </label>
        <div className="flex w-full flex-wrap items-center justify-end gap-2 sm:ml-auto sm:w-auto">
          <Button type="button" variant="ghost" size="sm" onClick={onClear} disabled={busy || total === 0} className="text-bg hover:bg-white/10 hover:text-bg">
            Clear
          </Button>
          {/* Opens a confirmation dialog; never deletes directly, so a stray Enter on the bar can't destroy data. */}
          <Button type="button" variant="destructive" size="sm" onClick={onDelete} disabled={busy || total === 0}>
            {deleting ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Trash2 className="size-3.5" aria-hidden />}
            Delete ({total})
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={onExport}
            disabled={busy || exportable === 0 || overCap}
            title={overCap ? `Export is limited to ${maxExport} evaluations` : exportable === 0 ? "None of the selected evaluations are completed with a score" : undefined}
          >
            {exporting ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Download className="size-3.5" aria-hidden />}
            {exporting ? "Preparing workbook…" : `Export to Excel (${exportable})`}
          </Button>
        </div>
      </div>
      {exportable < total && !busy && (
        <p className="text-xs opacity-80">
          {exportable} of {total} exportable — only completed evaluations with a score go into the workbook. Delete applies to all {total}.
        </p>
      )}
      {deleting && status.kind === "deleting" && (
        <p role="status" aria-live="polite" className="flex items-center gap-1.5 text-xs">
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          Deleting {status.done} of {status.total}…
        </p>
      )}
      {allMatching && exportable > maxExport && !busy && (
        <p className="text-xs opacity-80">Excel export includes the first {maxExport} of {exportable.toLocaleString()} matching completed evaluations.</p>
      )}
      {exportable > 100 && !busy && status.kind === "idle" && (
        <p className="text-xs opacity-80">Large exports can take ~10 seconds.</p>
      )}
      {status.kind === "success" && (
        <p role="status" className="flex min-w-0 items-start gap-1.5 break-words text-xs">
          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {status.message}
        </p>
      )}
      {status.kind === "error" && (
        <p role="alert" className="flex min-w-0 flex-wrap items-start gap-1.5 break-words text-xs">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span className="min-w-0 break-words">{status.message}</span>
          {status.action !== "delete" && (
            <button type="button" onClick={onExport} className="font-medium underline underline-offset-2">
              Retry
            </button>
          )}
        </p>
      )}
    </div>
  );
}
