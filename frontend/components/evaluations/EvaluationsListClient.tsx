"use client";

import { useMemo, useRef, useState } from "react";
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
import { deleteEvaluation, listEvaluations } from "@/lib/api-client";
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

      <SolidPanel className={cn("divide-y divide-hairline", visible.length === 0 && "hidden")}>
        {visible.map((evaluation) => (
          // `group relative` wraps the row's Link + its hover-reveal delete button as
          // SIBLINGS, not delete-button-inside-Link — nesting a <button> inside the <a> a
          // Link renders would be invalid, click-ambiguous HTML.
          <div key={evaluation.id} className="group relative">
            <Link
              href={`/charts/${evaluation.scorecardId}/evaluations/${evaluation.id}`}
              className="flex items-center justify-between gap-3 px-5 py-4 pr-12 transition-colors hover:bg-bg focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--focus)]"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="truncate text-sm font-medium text-ink">{evaluation.name}</p>
                  <Badge variant="muted">{evaluation.domain}</Badge>
                </div>
                <p className="mt-0.5 truncate text-xs text-ink-muted" suppressHydrationWarning>
                  {[evaluation.subjectName, evaluation.subjectEmail].filter(Boolean).length > 0 &&
                    `${[evaluation.subjectName, evaluation.subjectEmail].filter(Boolean).join(" · ")} · `}
                  {evaluation.scorecardName} · {evaluation.evaluatedByName} · {formatDateTime(evaluation.submittedAt)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                {/* An evaluation that never finished scoring has no real score — don't show it as "0.0 Critical". */}
                {evaluation.status === "completed" ? (
                  <RagBadge score={evaluation.finalWeightedScore} size="sm" />
                ) : (
                  <EvaluationStatusBadge evaluation={evaluation} queuePosition={queuePositions.get(evaluation.id)} />
                )}
                <ChevronRight className="size-4 text-ink-muted" aria-hidden />
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
                "absolute right-4 top-1/2 -translate-y-1/2 rounded-full p-1.5 text-ink-muted",
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
        ))}
      </SolidPanel>

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
        className="h-9 max-w-[18rem] rounded-lg border border-hairline bg-solid px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
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
