"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { EvaluationResultView } from "./EvaluationResultView";
import { GlassCard } from "@/components/design-system/GlassCard";
import { getCachedEvaluation, getScorecardWithVersion } from "@/lib/api-client";
import type { Evaluation, KpiNode } from "@/lib/types";

/**
 * Client-side fallback for evaluations that were created on the fly in the
 * Evaluate tab (see lib/api-client.ts createEvaluation). There's no real
 * backend yet, so those only exist in this browser tab's sessionStorage —
 * the server-rendered page can't see them, hence this small client loader.
 * Evaluations already in the static mock dataset never hit this path; they
 * render straight from the server component in page.tsx.
 */
export function EvaluationResultClientLoader({ scorecardId, evaluationId }: { scorecardId: string; evaluationId: string }) {
  const [state, setState] = useState<
    { status: "loading" } | { status: "not-found" } | { status: "ready"; evaluation: Evaluation; kpiNodes: KpiNode[] }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const cached = getCachedEvaluation(evaluationId);
      if (!cached) {
        if (!cancelled) setState({ status: "not-found" });
        return;
      }
      const withVersion = await getScorecardWithVersion(scorecardId);
      if (cancelled) return;
      if (!withVersion) {
        setState({ status: "not-found" });
        return;
      }
      setState({ status: "ready", evaluation: cached, kpiNodes: withVersion.version.kpiNodes });
    })();
    return () => {
      cancelled = true;
    };
  }, [scorecardId, evaluationId]);

  if (state.status === "loading") {
    return <p className="py-16 text-center text-sm text-ink-muted">Loading evaluation…</p>;
  }

  if (state.status === "not-found") {
    return (
      <GlassCard elevation={1} className="mx-auto mt-16 max-w-md p-6 text-center">
        <p className="text-sm font-medium text-ink">Evaluation not found</p>
        <p className="mt-1 text-sm text-ink-muted">
          This preview evaluation only exists in the browser tab it was created in. Run it again from the Evaluate
          tab, or open a saved evaluation from{" "}
          <Link href={`/charts/${scorecardId}`} className="underline">
            History
          </Link>
          .
        </p>
      </GlassCard>
    );
  }

  return <EvaluationResultView evaluation={state.evaluation} kpiNodes={state.kpiNodes} />;
}
