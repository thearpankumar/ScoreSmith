"use client";

import Link from "next/link";
import { Hourglass } from "lucide-react";

import { useLiveStatus } from "./LiveStatusProvider";
import type { MySlots } from "@/lib/collab-client";

/** Where "view it" goes for the user's running evaluation job (the batch page, or the evaluation's progress page). */
export function activeJobLink(job: NonNullable<MySlots["job"]>): string {
  if (job.batchId) return `/evaluations?batch=${job.batchId}`;
  if (job.scorecardId) return `/charts/${job.scorecardId}/evaluations/${job.evaluationId}`;
  return "/evaluations?status=active";
}

/**
 * "You already have an evaluation running - view it": shown wherever a new evaluation / batch could be started
 * while the signed-in user's single evaluation slot is busy. It disappears by itself when the job finishes (the live
 * status poll sees the slot free). Collaborators' jobs on the same chart do NOT block you.
 */
export function JobBusyNotice({ className }: { className?: string }) {
  const { slots } = useLiveStatus();
  if (!slots.job) return null;
  const isBatch = Boolean(slots.job.batchId);
  return (
    <p
      role="status"
      data-testid="job-busy-notice"
      className={`flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl bg-lemon-soft px-3 py-2 text-sm text-lemon-ink ${className ?? ""}`}
    >
      <Hourglass className="size-4 shrink-0" aria-hidden />
      <span>
        You already have {isBatch ? "a batch" : "an evaluation"} running. You can start another when it finishes.
      </span>
      <Link href={activeJobLink(slots.job)} className="min-h-11 py-2.5 font-semibold underline underline-offset-2 sm:min-h-0 sm:py-0">
        View it
      </Link>
    </p>
  );
}
