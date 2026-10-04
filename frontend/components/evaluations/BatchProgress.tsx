"use client";

import { useState } from "react";
import Link from "next/link";
import { Layers } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { getAiBatch } from "@/lib/api-client";
import { useAdaptivePoll } from "@/lib/useAdaptivePoll";
import type { AiBatch } from "@/lib/types";

/** Batch header: overall progress bar plus queued / running / done / failed counts; polls while work remains. */
export function BatchProgress({ batchId, initial }: { batchId: string; initial: AiBatch | null }) {
  const [batch, setBatch] = useState<AiBatch | null>(initial);

  const remaining = batch ? batch.counts.queued + batch.counts.running : 1;
  useAdaptivePoll(
    async (signal) => {
      const next = await getAiBatch(batchId, signal);
      if (next) setBatch(next);
    },
    remaining > 0,
    1.5,
  );

  if (!batch) {
    return (
      <GlassCard elevation={1} className="p-5 text-sm text-ink-muted">
        Loading batch...
      </GlassCard>
    );
  }

  const { counts, total } = batch;
  const finished = counts.completed + counts.failed;
  const allDone = remaining === 0;

  return (
    <GlassCard elevation={1} className="flex flex-col gap-3 p-5" aria-label="Batch progress">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-full bg-lemon text-lemon-ink">
            <Layers className="size-4" aria-hidden />
          </span>
          <div>
            <p className="text-sm font-semibold text-ink">
              {allDone ? "Batch finished" : "Batch in progress"}
              <span className="ml-2 font-normal text-ink-muted tabular-nums">
                {finished}/{total} done
              </span>
            </p>
          </div>
        </div>
        <Link href="/evaluations" className="text-xs text-ink-muted underline underline-offset-2 hover:text-ink">
          Show all evaluations
        </Link>
      </div>
      <Progress
        label="Batch progress"
        value={finished}
        max={Math.max(total, 1)}
        tone={allDone && counts.failed === 0 ? "ok" : "lemon"}
      />
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge variant="muted">Queued {counts.queued}</Badge>
        <Badge variant="soft">Running {counts.running}</Badge>
        <Badge variant="muted" className="text-[var(--rag-excellent)]">
          Completed {counts.completed}
        </Badge>
        <Badge variant="muted" className={counts.failed ? "bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]" : undefined}>
          Failed {counts.failed}
        </Badge>
      </div>
    </GlassCard>
  );
}
