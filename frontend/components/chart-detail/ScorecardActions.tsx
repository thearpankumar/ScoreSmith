"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Archive, ArchiveRestore, Loader2, PencilLine, Send, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { startRefineSession, updateScorecardStatus } from "@/lib/api-client";
import type { ScorecardStatus } from "@/lib/types";

type Transition = { to: ScorecardStatus; title: string; body: string; confirm: string };

/**
 * Chart-detail header actions:
 *  - "Refine with assistant": opens a NEW chat session pre-loaded with this scorecard's
 *    current version (see api-client startRefineSession). Opening it never needs the AI,
 *    so it works even when the assistant is unavailable; saving from that chat creates a
 *    new version of this scorecard.
 *  - Status lifecycle (draft <-> published, archive/restore) via the existing
 *    `PATCH /scorecards/{id}`. Previously nothing in the UI could change status, so a
 *    chat-created scorecard stayed "Draft" forever and a published one could never be
 *    edited directly.
 */
export function ScorecardActions({
  scorecardId,
  status,
  issues,
  evaluationCount,
}: {
  scorecardId: string;
  status: ScorecardStatus;
  /** Structural problems worth warning about before publishing (weights, missing guidelines). */
  issues: string[];
  evaluationCount: number;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<"refine" | "status" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Transition | null>(null);

  async function refine() {
    setBusy("refine");
    setError(null);
    try {
      const sessionId = await startRefineSession(scorecardId);
      router.push(`/chat/${sessionId}`);
    } catch (err) {
      setError(`Couldn't open the assistant: ${err instanceof Error ? err.message : String(err)}`);
      setBusy(null);
    }
  }

  async function applyStatus(to: ScorecardStatus) {
    setPending(null);
    setBusy("status");
    setError(null);
    try {
      await updateScorecardStatus(scorecardId, to);
      router.refresh();
    } catch (err) {
      setError(`Couldn't change the status: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  }

  const transitions: Array<Transition & { icon: typeof Send; variant: "default" | "outline" | "ghost" }> = [];
  if (status === "draft") {
    transitions.push({
      to: "published",
      title: "Publish this scorecard?",
      body: "Published scorecards are locked for direct editing so every evaluation is scored against the same structure. You can move it back to draft later.",
      confirm: "Publish",
      icon: Send,
      variant: "default",
    });
  }
  if (status === "published") {
    transitions.push({
      to: "draft",
      title: "Move back to draft?",
      body:
        evaluationCount > 0
          ? `This unlocks direct editing of the current version. ${evaluationCount} evaluation${evaluationCount === 1 ? " has" : "s have"} been scored against it — changing weights or names changes how those results read. To keep them exactly as scored, use “Refine with assistant” instead, which saves a new version.`
          : "This unlocks direct editing of the KPI structure on the Overview tab.",
      confirm: "Move to draft",
      icon: PencilLine,
      variant: "outline",
    });
  }
  if (status !== "archived") {
    transitions.push({
      to: "archived",
      title: "Archive this scorecard?",
      body: "Archived scorecards stay in the library (filter by status) and keep all their evaluations, but are read-only until restored.",
      confirm: "Archive",
      icon: Archive,
      variant: "ghost",
    });
  } else {
    transitions.push({
      to: "draft",
      title: "Restore to draft?",
      body: "The scorecard becomes editable again as a draft.",
      confirm: "Restore to draft",
      icon: ArchiveRestore,
      variant: "outline",
    });
  }

  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <div className="flex flex-wrap gap-2">
        {status !== "archived" && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={refine}
            disabled={busy !== null}
            title="Edit with AI: opens this scorecard in the Chat tab, already selected, so you can add/remove/reweight KPIs and rewrite guidelines by describing the change. (Looking to score something against it instead? Use the Evaluate tab.)"
          >
            {busy === "refine" ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Sparkles className="size-3.5" aria-hidden />}
            Refine with assistant
          </Button>
        )}
        {transitions.map((t) => (
          <Button
            key={t.to + t.confirm}
            type="button"
            size="sm"
            variant={t.variant}
            onClick={() => setPending(t)}
            disabled={busy !== null}
          >
            {busy === "status" ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <t.icon className="size-3.5" aria-hidden />}
            {t.confirm}
          </Button>
        ))}
      </div>
      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}

      <Dialog open={!!pending} onOpenChange={(open) => !open && setPending(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{pending?.title}</DialogTitle>
            <DialogDescription>{pending?.body}</DialogDescription>
          </DialogHeader>
          {pending?.to === "published" && issues.length > 0 && (
            <div className="rounded-lg border border-[var(--rag-poor)]/30 bg-[var(--rag-poor)]/5 p-3 text-sm text-ink">
              <p className="mb-1 flex items-center gap-1.5 font-medium">
                <AlertTriangle className="size-4 text-[var(--rag-poor)]" aria-hidden />
                This scorecard still has gaps:
              </p>
              <ul className="list-disc space-y-0.5 pl-5 text-xs">
                {issues.map((i) => (
                  <li key={i}>{i}</li>
                ))}
              </ul>
              <p className="mt-1.5 text-xs text-ink-muted">You can publish anyway, but scoring may be incomplete.</p>
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button type="button" onClick={() => pending && applyStatus(pending.to)}>
              {pending?.confirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
