import { Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { isActiveStatus, statusLabel } from "@/lib/eval-status";
import { cn } from "@/lib/utils";
import type { Evaluation } from "@/lib/types";

/** Status pill for a not-yet-scored (or failed) evaluation: Queued #n, Fetching, Extracting, Scoring, Failed... */
export function EvaluationStatusBadge({
  evaluation,
  queuePosition,
}: {
  evaluation: Pick<Evaluation, "status" | "stage" | "errorCode">;
  queuePosition?: number | null;
}) {
  const { label, tone } = statusLabel(evaluation, queuePosition);
  const active = isActiveStatus(evaluation.status) && evaluation.status !== "queued";
  return (
    <Badge
      variant={tone === "lemon" ? "lemon" : tone === "soft" ? "soft" : "muted"}
      className={cn(tone === "danger" && "bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]")}
    >
      {active && <Loader2 className="size-3 animate-spin" aria-hidden />}
      {label}
    </Badge>
  );
}
