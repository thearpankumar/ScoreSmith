"use client";

import { useState } from "react";
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
import { deleteEvaluation } from "@/lib/api-client";
import { cn, formatDateTime } from "@/lib/utils";
import type { Evaluation } from "@/lib/types";

/**
 * Client-side wrapper for the Evaluations list (moved out of `app/evaluations/page.tsx`,
 * a Server Component, so a hover-delete can remove a row from the visible list
 * immediately on success, matching the Charts library grid / chat SessionList pattern).
 */
export function EvaluationsListClient({ evaluations: initialEvaluations }: { evaluations: Evaluation[] }) {
  const [evaluations, setEvaluations] = useState<Evaluation[]>(initialEvaluations);
  const [pendingDelete, setPendingDelete] = useState<Evaluation | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteEvaluation(pendingDelete.id);
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
      <SolidPanel className="divide-y divide-hairline">
        {evaluations.map((evaluation) => (
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
                  {evaluation.scorecardName} · {evaluation.evaluatedByName} · {formatDateTime(evaluation.submittedAt)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                {/* An evaluation that never finished scoring has no real score — don't show it as "0.0 Critical". */}
                {evaluation.status === "completed" ? (
                  <RagBadge score={evaluation.finalWeightedScore} size="sm" />
                ) : (
                  <Badge variant="muted">
                    {evaluation.status === "failed" ? "Not scored" : evaluation.status === "pending" ? "Pending" : "In progress"}
                  </Badge>
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
