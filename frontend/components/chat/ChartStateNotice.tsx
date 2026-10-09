"use client";

import { useState } from "react";
import Link from "next/link";
import { ArchiveRestore, ArrowRight, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { restoreFromTrash, TRASH_CHANGED_EVENT } from "@/lib/trash-client";
import type { ChatSession } from "@/lib/types";

/**
 * Inline notice for a chat whose chart is in the trash or was deleted, so the chat never leads to a 404 dead end.
 * - trashed + owner: "in the trash" with a Restore button (then a normal link to the chart appears);
 * - trashed + someone else (an editor): plain text, no link and no button (they cannot open or restore it);
 * - deleted: the chart no longer exists.
 */
export function ChartStateNotice({ session }: { session: Pick<ChatSession, "chartState" | "trashedChartId"> }) {
  const [phase, setPhase] = useState<"idle" | "busy" | "restored" | "error">("idle");
  const state = session.chartState;
  if (state !== "trashed" && state !== "deleted") return null;

  if (phase === "restored" && session.trashedChartId) {
    return (
      <div
        role="status"
        className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline bg-lemon-soft/60 px-4 py-2.5 text-xs text-ink sm:px-5"
        data-testid="chart-state-notice"
      >
        <p className="flex min-w-0 items-center gap-1.5">
          <ArchiveRestore className="size-3.5 shrink-0 text-lemon-ink" aria-hidden />
          <span>The chart was restored.</span>
        </p>
        <Button asChild size="sm" variant="outline">
          <Link href={`/charts/${session.trashedChartId}`}>
            Open chart
            <ArrowRight className="size-3.5" aria-hidden />
          </Link>
        </Button>
      </div>
    );
  }

  const canRestore = state === "trashed" && !!session.trashedChartId;
  const text =
    state === "deleted"
      ? "The chart built in this chat was deleted. The conversation is still here, but there is no chart to open."
      : canRestore
        ? "The chart built in this chat is in the trash. Restore it to open it again."
        : "The chart behind this chat is in its owner's trash, so it can't be opened right now.";

  async function restore() {
    if (!session.trashedChartId) return;
    setPhase("busy");
    try {
      const res = await restoreFromTrash([session.trashedChartId]);
      if (res.done.length === 0) throw new Error("not restored");
      window.dispatchEvent(new Event(TRASH_CHANGED_EVENT));
      setPhase("restored");
    } catch {
      setPhase("error");
    }
  }

  return (
    <div
      role="status"
      className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline bg-lemon-soft/60 px-4 py-2.5 text-xs text-ink sm:px-5"
      data-testid="chart-state-notice"
      data-chart-state={state}
    >
      <p className="flex min-w-0 items-start gap-1.5">
        <Trash2 className="mt-0.5 size-3.5 shrink-0 text-lemon-ink" aria-hidden />
        <span className="min-w-0 break-words">
          {text}
          {phase === "error" && <span className="ml-1 text-[var(--rag-poor)]">Could not restore it - try again.</span>}
        </span>
      </p>
      {canRestore && (
        <Button type="button" size="sm" onClick={restore} disabled={phase === "busy"}>
          {phase === "busy" ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <ArchiveRestore className="size-3.5" aria-hidden />
          )}
          Restore chart
        </Button>
      )}
    </div>
  );
}
