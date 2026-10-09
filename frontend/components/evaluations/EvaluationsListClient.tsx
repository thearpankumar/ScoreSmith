"use client";

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronRight, Loader2, Trash2 } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { RagBadge } from "@/components/design-system/RagBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EvaluationStatusBadge } from "./EvaluationStatusBadge";
import { EvaluationSelectionBar, type ExportStatus } from "./EvaluationSelectionBar";
import { deleteEvaluation, exportEvaluationsXlsx, listEvaluations } from "@/lib/api-client";
import { saveBlob } from "@/lib/download";
import {
  counts,
  deselectMany,
  describeFilters,
  exportableSelection,
  headerState,
  isSelectable,
  previewNames,
  prune,
  rangeSelect,
  selectMany,
  selectableIds,
  selectionInOrder,
  toggle,
} from "@/lib/eval-selection";
import { runBulkDelete, summarizeFailures } from "@/lib/bulk-delete";
import { usePersistedState } from "@/lib/usePersistedState";
import {
  ALL,
  NO_FILTERS,
  STATUS_OPTIONS,
  filterEvaluations,
  hasActiveFilters,
  sanitizeScorecardFilter,
  scorecardOptions,
  type EvaluationFilters,
  type StatusFilter,
} from "@/lib/eval-filters";
import { isActiveStatus } from "@/lib/eval-status";
import { useAdaptivePoll } from "@/lib/useAdaptivePoll";
import { cn, formatDateTime } from "@/lib/utils";
import type { Evaluation } from "@/lib/types";

/**
 * Client-side wrapper for the Evaluations list (moved out of `app/evaluations/page.tsx`,
 * a Server Component, so a hover-delete can remove a row from the visible list
 * immediately on success, matching the Charts library grid / chat SessionList pattern).
 */
export function EvaluationsListClient({
  evaluations: initialEvaluations,
  batchId,
  initialFilters = NO_FILTERS,
}: {
  evaluations: Evaluation[];
  /** When set, only that batch's evaluations are listed. */
  batchId?: string;
  /** Filters from the URL (`?scorecard=&status=`), so a filtered view can be bookmarked or shared. */
  initialFilters?: EvaluationFilters;
}) {
  const [all, setAll] = useState<Evaluation[]>(initialEvaluations);
  const deletedIds = useRef(new Set<string>());
  const evaluations = useMemo(() => (batchId ? all.filter((e) => e.batchId === batchId) : all), [all, batchId]);
  const setEvaluations = setAll;

  // Filter by workflow (the scorecard an evaluation ran against) and by status.
  const options = useMemo(() => scorecardOptions(evaluations), [evaluations]);
  const [filters, setFilters] = useState<EvaluationFilters>(initialFilters);
  // A scorecard that no longer has evaluations (deleted) must not leave the list empty behind a stale filter.
  const effective = useMemo<EvaluationFilters>(
    () => ({ ...filters, scorecardId: sanitizeScorecardFilter(filters.scorecardId, options) }),
    [filters, options],
  );
  const visible = useMemo(() => filterEvaluations(evaluations, effective), [evaluations, effective]);

  // ---- Multi-select + Excel export ----------------------------------------------------------------------
  // The whole list is one page (capped at 200), so "select all" over the filtered view is every selectable
  // visible row. Selection survives filter changes (hidden selected rows stay selected and are counted).
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [includeReasoning, setIncludeReasoning] = usePersistedState("evaluations.export.includeReasoning", true);
  const [exportStatus, setExportStatus] = useState<ExportStatus>({ kind: "idle" });
  const [bulkOpen, setBulkOpen] = useState(false);
  const anchorRef = useRef<string | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const exportAbort = useRef<AbortController | null>(null);
  const headerRef = useRef<HTMLInputElement>(null);

  const allSelectable = useMemo(() => selectableIds(evaluations), [evaluations]);
  const visibleSelectable = useMemo(() => selectableIds(visible), [visible]);
  const hState = headerState(visibleSelectable, selected);
  const sel = counts(selected, visibleSelectable);
  const hiddenSelectable = allSelectable.length - visibleSelectable.length;
  const exportableCount = exportableSelection(selected, evaluations).length;
  const bulkBusy = exportStatus.kind === "exporting" || exportStatus.kind === "deleting";
  const allVisibleSelected = hState === "all";

  // Drop ids that vanished or stopped being selectable (list refresh, delete).
  useEffect(() => {
    setSelected((prev) => prune(prev, allSelectable));
  }, [allSelectable]);

  useEffect(() => {
    if (headerRef.current) headerRef.current.indeterminate = hState === "some";
  }, [hState]);

  // The success note fades after a few seconds.
  useEffect(() => {
    if (exportStatus.kind !== "success") return;
    const t = setTimeout(() => setExportStatus({ kind: "idle" }), 6000);
    return () => clearTimeout(t);
  }, [exportStatus]);

  useEffect(() => () => exportAbort.current?.abort(), []);

  function onHeaderToggle() {
    setSelected((prev) => (hState === "all" ? deselectMany(prev, visibleSelectable) : selectMany(prev, visibleSelectable)));
    anchorRef.current = null;
  }

  function onRowToggle(id: string, shift: boolean) {
    setSelected((prev) => {
      const willSelect = !prev.has(id);
      return shift && anchorRef.current
        ? rangeSelect(prev, visibleSelectable, anchorRef.current, id, willSelect)
        : toggle(prev, id);
    });
    anchorRef.current = id;
  }

  function clearSelection() {
    setSelected(new Set());
    anchorRef.current = null;
  }

  function filterSummary(): string | undefined {
    return describeFilters({
      workflow: effective.scorecardId === ALL ? null : (options.find((o) => o.id === effective.scorecardId)?.name ?? null),
      status: effective.status === ALL ? null : (STATUS_OPTIONS.find((o) => o.value === effective.status)?.label ?? null),
    });
  }

  async function runExport() {
    const ids = exportableSelection(selected, evaluations);
    if (ids.length === 0 || bulkBusy) return;
    const controller = new AbortController();
    exportAbort.current = controller;
    const timeout = setTimeout(() => controller.abort(), 90_000);
    setExportStatus({ kind: "exporting" });
    try {
      const result = await exportEvaluationsXlsx(ids, {
        includeReasoning,
        filterSummary: filterSummary(),
        signal: controller.signal,
      });
      saveBlob(result.blob, result.filename);
      const skipped =
        result.skipped > 0
          ? ` ${result.skipped} evaluation${result.skipped === 1 ? " was" : "s were"} skipped (deleted or not completed).`
          : "";
      setExportStatus({
        kind: "success",
        message: `Downloaded ${result.filename} (${result.count} evaluation${result.count === 1 ? "" : "s"}).${skipped}`,
      });
    } catch (err) {
      const message = controller.signal.aborted
        ? "The export took too long and was cancelled. Try a smaller selection."
        : err instanceof Error
          ? err.message
          : "Couldn't export the evaluations. Try again.";
      setExportStatus({ kind: "error", message });
    } finally {
      clearTimeout(timeout);
    }
  }

  /** Deletes the whole selection (completed + failed rows) after the confirmation dialog. */
  async function runBulkDeleteSelection() {
    const ids = selectionInOrder(selected, evaluations.map((e) => e.id));
    setBulkOpen(false);
    if (ids.length === 0 || bulkBusy) return;
    setExportStatus({ kind: "deleting", done: 0, total: ids.length });
    const result = await runBulkDelete(ids, deleteEvaluation, {
      concurrency: 4,
      onProgress: (done, total) => setExportStatus({ kind: "deleting", done, total }),
    });
    // Drop what was deleted from the list and the selection right away; failures stay listed AND selected.
    if (result.succeeded.length > 0) {
      const gone = new Set(result.succeeded);
      for (const id of gone) deletedIds.current.add(id);
      setEvaluations((prev) => prev.filter((e) => !gone.has(e.id)));
      setSelected((prev) => deselectMany(prev, result.succeeded));
    }
    if (result.failed.length === 0) {
      setExportStatus({
        kind: "success",
        message: `Deleted ${result.succeeded.length} evaluation${result.succeeded.length === 1 ? "" : "s"}.`,
      });
    } else {
      const ok = result.succeeded.length > 0 ? `Deleted ${result.succeeded.length}. ` : "";
      setExportStatus({ kind: "error", action: "delete", message: `${ok}${summarizeFailures(result.failed)} They are still selected.` });
    }
  }

  function changeFilters(next: EvaluationFilters) {
    setFilters(next);
    // Keep the address bar in step without a navigation (no re-fetch, no scroll jump).
    const url = new URL(window.location.href);
    if (next.scorecardId === ALL) url.searchParams.delete("scorecard");
    else url.searchParams.set("scorecard", next.scorecardId);
    if (next.status === ALL) url.searchParams.delete("status");
    else url.searchParams.set("status", next.status);
    window.history.replaceState(null, "", url.toString());
  }

  // Poll while anything is queued / running so badges and scores fill in live.
  const hasActive = all.some((e) => isActiveStatus(e.status));
  useAdaptivePoll(
    async () => {
      const rows = await listEvaluations();
      setAll(rows.filter((e) => !deletedIds.current.has(e.id)).sort((a, b) => b.submittedAt.localeCompare(a.submittedAt)));
    },
    hasActive,
    2,
  );

  // 1-based position among queued rows, oldest queued first.
  const queuePositions = useMemo(() => {
    const queued = all
      .filter((e) => e.status === "queued")
      .sort((a, b) => (a.queuedAt ?? a.submittedAt).localeCompare(b.queuedAt ?? b.submittedAt));
    return new Map(queued.map((e, i) => [e.id, i + 1]));
  }, [all]);
  const [pendingDelete, setPendingDelete] = useState<Evaluation | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteEvaluation(pendingDelete.id);
      deletedIds.current.add(pendingDelete.id);
      setEvaluations((prev) => prev.filter((e) => e.id !== pendingDelete.id));
      setPendingDelete(null);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : "Couldn't delete this evaluation. Try again.");
    } finally {
      setDeleting(false);
    }
  }

  if (evaluations.length === 0) {
    return <SolidPanel className="p-6 text-sm text-ink-muted">No evaluations yet.</SolidPanel>;
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter evaluations">
        <FilterSelect
          label="Workflow (scorecard)"
          value={effective.scorecardId}
          onChange={(scorecardId) => changeFilters({ ...effective, scorecardId })}
          options={[
            { value: ALL, label: `All workflows (${evaluations.length})` },
            ...options.map((o) => ({ value: o.id, label: `${o.name} (${o.count})` })),
          ]}
        />
        <FilterSelect
          label="Status"
          value={effective.status}
          onChange={(status) => changeFilters({ ...effective, status: status as StatusFilter })}
          options={STATUS_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
        />
        {hasActiveFilters(effective) && (
          <>
            <span className="text-xs text-ink-muted tabular-nums" aria-live="polite">
              Showing {visible.length} of {evaluations.length}
            </span>
            <Button type="button" variant="ghost" size="sm" onClick={() => changeFilters(NO_FILTERS)}>
              Clear filters
            </Button>
          </>
        )}
      </div>

      {visible.length === 0 && (
        <SolidPanel className="p-6 text-sm text-ink-muted">
          No evaluations match these filters.{" "}
          <button type="button" className="underline underline-offset-2 hover:text-ink" onClick={() => changeFilters(NO_FILTERS)}>
            Clear filters
          </button>
        </SolidPanel>
      )}

      {allSelectable.length > 0 && visible.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 py-0.5 text-xs text-ink-muted">
          <label className="flex cursor-pointer items-center gap-2">
            <input
              ref={headerRef}
              type="checkbox"
              checked={allVisibleSelected}
              aria-checked={hState === "some" ? "mixed" : allVisibleSelected}
              onChange={onHeaderToggle}
              disabled={visibleSelectable.length === 0}
              aria-label={`Select all ${visibleSelectable.length} completed or failed evaluations in this view`}
              className="size-4 accent-[var(--ink)]"
            />
            <span>Select all in view ({visibleSelectable.length})</span>
          </label>
          {evaluations.length >= 200 && <span>Showing your 200 most recent evaluations</span>}
          {allVisibleSelected && hiddenSelectable > 0 && sel.hidden < hiddenSelectable && (
            <span aria-live="polite" className="min-w-0">
              All {visibleSelectable.length} selectable evaluations in this view are selected.{" "}
              <button
                type="button"
                className="underline underline-offset-2 hover:text-ink"
                onClick={() => setSelected((prev) => selectMany(prev, allSelectable))}
              >
                Select all {allSelectable.length} evaluations
              </button>
            </span>
          )}
          {selected.size === allSelectable.length && hiddenSelectable > 0 && (
            <span aria-live="polite">
              All {allSelectable.length} selectable evaluations are selected.{" "}
              <button type="button" className="underline underline-offset-2 hover:text-ink" onClick={clearSelection}>
                Clear selection
              </button>
            </span>
          )}
        </div>
      )}

      <SolidPanel
        className={cn("divide-y divide-hairline", visible.length === 0 && "hidden")}
        onKeyDown={(e: KeyboardEvent<HTMLDivElement>) => {
          if (e.key === "Escape" && selected.size > 0) clearSelection();
        }}
      >
        {visible.map((evaluation) => {
          const selectable = isSelectable(evaluation);
          const isSelected = selected.has(evaluation.id);
          return (
          // `group relative` wraps the row's Link + its hover-reveal delete button as
          // SIBLINGS, not delete-button-inside-Link — nesting a <button> inside the <a> a
          // Link renders would be invalid, click-ambiguous HTML. The selection checkbox is a sibling too.
          <div
            key={evaluation.id}
            // Always reserve the 4px accent edge (transparent when unselected) so selecting never shifts the row content.
            className={cn("group relative border-l-4 border-l-transparent", isSelected && "border-l-[var(--ink)] bg-[var(--lemon-soft)]")}
          >
            <input
              type="checkbox"
              checked={isSelected}
              disabled={!selectable}
              onChange={() => undefined}
              onClick={(e) => onRowToggle(evaluation.id, e.shiftKey)}
              aria-label={`Select evaluation “${evaluation.name}”`}
              title={selectable ? undefined : "Only completed or failed evaluations can be selected"}
              className="absolute left-3 top-1/2 z-10 size-4 -translate-y-1/2 accent-[var(--ink)] disabled:cursor-not-allowed disabled:opacity-40"
            />
            <Link
              href={`/charts/${evaluation.scorecardId}/evaluations/${evaluation.id}`}
              className="flex items-center justify-between gap-3 py-4 pl-11 pr-10 transition-colors sm:pr-12 hover:bg-bg focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--focus)]"
            >
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <p className="min-w-0 truncate text-sm font-medium text-ink">{evaluation.name}</p>
                  <Badge variant="muted" className="shrink-0">{evaluation.domain}</Badge>
                </div>
                <p className="mt-0.5 truncate text-xs text-ink-muted" suppressHydrationWarning>
                  {[evaluation.subjectName, evaluation.subjectEmail].filter(Boolean).length > 0 &&
                    `${[evaluation.subjectName, evaluation.subjectEmail].filter(Boolean).join(" · ")} · `}
                  {evaluation.scorecardName} · {evaluation.evaluatedByName} · {formatDateTime(evaluation.submittedAt)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2 sm:gap-3">
                {/* An evaluation that never finished scoring has no real score — don't show it as "0.0 Critical". */}
                {evaluation.status === "completed" ? (
                  <RagBadge score={evaluation.finalWeightedScore} size="sm" target={evaluation.targetScore} />
                ) : (
                  <EvaluationStatusBadge evaluation={evaluation} queuePosition={queuePositions.get(evaluation.id)} />
                )}
                <ChevronRight className="hidden size-4 text-ink-muted sm:block" aria-hidden />
              </div>
            </Link>
            <button
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setDeleteError(null);
                setPendingDelete(evaluation);
              }}
              aria-label={`Delete evaluation “${evaluation.name}”`}
              title="Delete evaluation"
              className={cn(
                "absolute right-2 top-1/2 -translate-y-1/2 rounded-full p-1.5 text-ink-muted sm:right-4",
                "opacity-0 transition-opacity hover:bg-[var(--rag-poor)]/10 hover:text-[var(--rag-poor)]",
                // Hover-reveal, but visible on keyboard focus too (group-focus-within /
                // focus-visible — hover-only is unreachable without a mouse) and always
                // visible on touch/coarse-pointer devices (no hover concept there).
                "group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100",
                "[@media(hover:none)]:opacity-100",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
              )}
            >
              <Trash2 className="size-3.5" aria-hidden />
            </button>
          </div>
          );
        })}
      </SolidPanel>

      <EvaluationSelectionBar
        total={sel.total}
        exportable={exportableCount}
        hidden={sel.hidden}
        includeReasoning={includeReasoning}
        onIncludeReasoning={setIncludeReasoning}
        status={exportStatus}
        onExport={runExport}
        onDelete={() => setBulkOpen(true)}
        onClear={clearSelection}
        onDeselectHidden={() => setSelected((prev) => new Set([...prev].filter((id) => visibleSelectable.includes(id))))}
      />

      <Dialog open={!!pendingDelete} onOpenChange={(open) => !open && !deleting && setPendingDelete(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{pendingDelete?.name ?? "this evaluation"}”?</DialogTitle>
            <DialogDescription>This permanently deletes the evaluation and its results. This can&apos;t be undone.</DialogDescription>
          </DialogHeader>
          {deleteError && (
            <p role="alert" className="flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {deleteError}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setPendingDelete(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button type="button" variant="destructive" onClick={confirmDelete} disabled={deleting}>
              {deleting && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={bulkOpen} onOpenChange={setBulkOpen}>
        <DialogContent
          // Cancel is the default focus, so Enter/Space on open can never confirm a destructive delete.
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            cancelRef.current?.focus();
          }}
        >
          <BulkDeleteBody
            names={evaluations.filter((e) => selected.has(e.id)).map((e) => e.name)}
            cancelRef={cancelRef}
            onCancel={() => setBulkOpen(false)}
            onConfirm={runBulkDeleteSelection}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}

function BulkDeleteBody({
  names,
  cancelRef,
  onCancel,
  onConfirm,
}: {
  names: string[];
  cancelRef: RefObject<HTMLButtonElement | null>;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { shown, more } = previewNames(names, 5);
  return (
    <>
      <DialogHeader>
        <DialogTitle>
          Delete {names.length} evaluation{names.length === 1 ? "" : "s"}?
        </DialogTitle>
        <DialogDescription>
          This permanently deletes the selected evaluation{names.length === 1 ? "" : "s"} and their results. This can&apos;t be undone.
        </DialogDescription>
      </DialogHeader>
      <ul className="list-disc space-y-0.5 pl-5 text-sm text-ink">
        {shown.map((name, i) => (
          <li key={`${i}-${name}`} className="break-words">
            {name}
          </li>
        ))}
        {more > 0 && <li className="list-none text-ink-muted">…and {more} more</li>}
      </ul>
      <DialogFooter>
        <Button ref={cancelRef} type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" variant="destructive" onClick={onConfirm}>
          <Trash2 className="size-3.5" aria-hidden />
          Delete {names.length}
        </Button>
      </DialogFooter>
    </>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs font-medium text-ink-muted">
      <span className="sr-only">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className="h-9 max-w-[18rem] glass-field rounded-lg px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}
