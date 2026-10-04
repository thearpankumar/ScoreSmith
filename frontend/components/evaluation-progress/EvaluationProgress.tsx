"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  CircleDashed,
  Loader2,
  MinusCircle,
  RotateCcw,
  WifiOff,
  XCircle,
} from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { SolidPanel } from "@/components/design-system/SolidPanel";
import { EvaluationStatusBadge } from "@/components/evaluations/EvaluationStatusBadge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cancelEvaluation, retryEvaluation, watchEvaluation } from "@/lib/api-client";
import { PIPELINE_STEPS, errorInfo, isActiveStatus, stepIndex } from "@/lib/eval-status";
import { cn } from "@/lib/utils";
import type { Evaluation, EvaluationProgress as ProgressSnapshot, ProgressFileState } from "@/lib/types";

const FILE_STATE: Record<ProgressFileState, { label: string; icon: typeof Circle; cls: string }> = {
  pending: { label: "Waiting", icon: CircleDashed, cls: "text-ink-muted" },
  running: { label: "In progress", icon: Loader2, cls: "text-ink animate-spin" },
  done: { label: "Done", icon: CheckCircle2, cls: "text-[var(--rag-excellent)]" },
  skipped: { label: "Skipped", icon: MinusCircle, cls: "text-[var(--rag-needs-improvement)]" },
  failed: { label: "Failed", icon: XCircle, cls: "text-[var(--rag-poor)]" },
};

/**
 * Live view of a queued / running / failed AI evaluation. Polls `GET /evaluations/{id}/progress`
 * via `watchEvaluation`, shows the stage stepper, per-file rows, counters and event log, and offers
 * Cancel and Retry. When the run completes it refreshes the route so the server component swaps in
 * the normal result view.
 */
export function EvaluationProgress({
  evaluation,
  embedded = false,
  onFinished,
  onNew,
}: {
  evaluation: Evaluation;
  /** Rendered inside another view (the Evaluate tab) instead of as its own page: no page chrome, and no
   * route refresh on completion, the host decides what to show next via `onFinished`. */
  embedded?: boolean;
  onFinished?: (final: ProgressSnapshot) => void;
  /** Embedded only: go back to the input form (offered after a failure). */
  onNew?: () => void;
}) {
  const router = useRouter();
  const finishedRef = useRef(onFinished);
  useEffect(() => {
    finishedRef.current = onFinished;
  });
  const [snap, setSnap] = useState<ProgressSnapshot | null>(null);
  const [watchKey, setWatchKey] = useState(0);
  const [offline, setOffline] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const [acting, setActing] = useState<"cancel" | "retry" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const furthest = useRef(-1);

  useEffect(() => {
    const ac = new AbortController();
    setFatal(null);
    watchEvaluation(evaluation.id, {
      signal: ac.signal,
      onUpdate: setSnap,
      onConnection: (ok) => setOffline(!ok),
    })
      .then((final) => {
        if (embedded) finishedRef.current?.(final);
        else if (final.status === "completed") router.refresh();
      })
      .catch((err) => {
        if (ac.signal.aborted) return;
        setFatal(err instanceof Error ? err.message : "Lost track of this evaluation.");
      });
    return () => ac.abort();
  }, [evaluation.id, watchKey, router, embedded]);

  const status = snap?.status ?? evaluation.status;
  const stage = snap?.stage ?? evaluation.stage ?? null;
  const errorCode = snap?.errorCode ?? evaluation.errorCode ?? null;
  const failed = status === "failed";
  const active = isActiveStatus(status);

  const idx = stepIndex(status, snap?.progress?.stage || stage);
  if (idx > furthest.current && idx < 4) furthest.current = idx;
  const failedAt = failed ? Math.max(furthest.current, 0) : -1;

  async function act(kind: "cancel" | "retry") {
    setActing(kind);
    setActionError(null);
    try {
      if (kind === "cancel") await cancelEvaluation(evaluation.id);
      else {
        await retryEvaluation(evaluation.id);
        furthest.current = -1;
        setSnap(snap ? { ...snap, status: "queued", stage: "queued", errorCode: null, errorMessage: null } : null);
        setWatchKey((k) => k + 1);
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `Could not ${kind} this evaluation.`);
    } finally {
      setActing(null);
    }
  }

  const progress = snap?.progress ?? null;
  const counters = progress?.counters;
  const bars = counters
    ? [
        { label: "Files", done: counters.filesDone, total: counters.filesTotal },
        { label: "Images analysed", done: counters.imagesDone, total: counters.imagesTotal },
        { label: "Audio chunks transcribed", done: counters.chunksDone, total: counters.chunksTotal },
      ].filter((b) => b.total > 0)
    : [];
  const files = progress?.files ?? [];
  const warnedSources = (snap?.sources ?? []).filter((s) => s.warnings.length > 0);
  const events = [...(snap?.events ?? [])].reverse();
  const err = failed ? errorInfo(errorCode) : null;
  const subject = [evaluation.subjectName, evaluation.subjectEmail].filter(Boolean).join(" · ");
  const Heading = embedded ? "h2" : "h1";

  return (
    <div className={cn("flex w-full flex-col gap-4", !embedded && "mx-auto max-w-4xl py-2")}>
      <GlassCard elevation={embedded ? 1 : 2} className="flex flex-col gap-5 p-5 sm:p-7">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">AI evaluation</p>
            <Heading className="mt-1 break-words text-xl font-semibold text-ink sm:text-2xl">{evaluation.name}</Heading>
            <p className="mt-1 text-sm text-ink-muted">
              {subject && <>{subject} · </>}
              {evaluation.scorecardName}
            </p>
          </div>
          <EvaluationStatusBadge
            evaluation={{ status, stage, errorCode }}
            queuePosition={snap?.queuePosition}
          />
        </div>

        <ol className="flex items-start" aria-label="Pipeline progress">
          {PIPELINE_STEPS.map((step, i) => {
            const done = failed ? i < failedAt : i < idx;
            const current = failed ? i === failedAt : i === idx;
            return (
              <li key={step} className="relative flex min-w-0 flex-1 flex-col items-center gap-1.5 text-center">
                {i > 0 && (
                  <span
                    className={cn("absolute right-1/2 top-3.5 h-0.5 w-full", done || current ? "bg-ink/30" : "bg-hairline")}
                    aria-hidden
                  />
                )}
                <span
                  className={cn(
                    "relative z-10 flex size-7 items-center justify-center rounded-full border text-xs font-semibold",
                    done && "border-transparent bg-[var(--rag-excellent)] text-white",
                    current && !failed && "border-transparent bg-lemon text-lemon-ink",
                    current && failed && "border-transparent bg-[var(--rag-poor)] text-white",
                    !done && !current && "border-hairline bg-solid text-ink-muted",
                  )}
                  aria-current={current && !failed ? "step" : undefined}
                >
                  {done ? (
                    <CheckCircle2 className="size-4" aria-hidden />
                  ) : current && failed ? (
                    <XCircle className="size-4" aria-hidden />
                  ) : current && active ? (
                    <Loader2 className="size-4 animate-spin" aria-hidden />
                  ) : (
                    i + 1
                  )}
                </span>
                <span className={cn("text-xs font-medium", current || done ? "text-ink" : "text-ink-muted")}>{step}</span>
              </li>
            );
          })}
        </ol>

        <div aria-live="polite" className="text-sm text-ink-muted">
          {status === "queued" ? (
            snap?.queuePosition ? (
              <>
                Waiting in the queue: <span className="font-semibold text-ink">#{snap.queuePosition}</span>. It starts
                automatically when a slot frees up.
              </>
            ) : (
              "Waiting in the queue. It starts automatically when a slot frees up."
            )
          ) : active ? (
            progress?.message || "Working on it..."
          ) : null}
        </div>

        {bars.length > 0 && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {bars.map((b) => (
              <div key={b.label} className="flex flex-col gap-1">
                <div className="flex justify-between text-xs text-ink-muted">
                  <span>{b.label}</span>
                  <span className="tabular-nums">
                    {b.done}/{b.total}
                  </span>
                </div>
                <Progress label={b.label} value={b.done} max={b.total} />
              </div>
            ))}
          </div>
        )}

        {err && (
          <div role="alert" className="flex items-start gap-3 rounded-xl bg-[var(--rag-poor)]/10 p-4">
            <AlertTriangle className="mt-0.5 size-5 shrink-0 text-[var(--rag-poor)]" aria-hidden />
            <div className="min-w-0 text-sm">
              <p className="font-semibold text-ink">{err.title}</p>
              <p className="mt-0.5 text-ink-muted">{err.hint}</p>
              {(snap?.errorMessage ?? evaluation.errorMessage) && errorCode !== "cancelled" && (
                <p className="mt-2 break-words font-mono text-xs text-ink-muted">
                  {snap?.errorMessage ?? evaluation.errorMessage}
                </p>
              )}
            </div>
          </div>
        )}

        {(offline || fatal) && (
          <p role="status" className="flex items-center gap-2 text-xs text-ink-muted">
            <WifiOff className="size-4" aria-hidden />
            {fatal ?? "Connection lost. Retrying; your evaluation keeps running."}
          </p>
        )}
        {actionError && (
          <p role="alert" className="text-sm text-[var(--rag-poor)]">
            {actionError}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2">
          {(status === "queued" || active) && (
            <Button type="button" variant="outline" onClick={() => act("cancel")} disabled={acting !== null}>
              {acting === "cancel" && <Loader2 className="animate-spin" aria-hidden />}
              Cancel
            </Button>
          )}
          {failed && (
            <Button type="button" onClick={() => act("retry")} disabled={acting !== null}>
              {acting === "retry" ? <Loader2 className="animate-spin" aria-hidden /> : <RotateCcw aria-hidden />}
              Retry
            </Button>
          )}
          {embedded ? (
            <>
              {(failed || fatal) && onNew && (
                <Button type="button" variant="ghost" onClick={onNew}>
                  Start over
                </Button>
              )}
              <Button asChild variant="ghost">
                <Link href={`/charts/${evaluation.scorecardId}/evaluations/${evaluation.id}`}>Open full page</Link>
              </Button>
            </>
          ) : (
            <>
              <Button asChild variant="ghost">
                <Link href={`/charts/${evaluation.scorecardId}`}>Back to scorecard</Link>
              </Button>
              <Button asChild variant="ghost">
                <Link href="/evaluations">All evaluations</Link>
              </Button>
            </>
          )}
        </div>
      </GlassCard>

      {files.length > 0 && (
        <SolidPanel className="p-4 sm:p-5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Files</h2>
          <ul className="mt-3 divide-y divide-hairline">
            {files.map((f) => {
              const st = FILE_STATE[f.state] ?? FILE_STATE.pending;
              const Icon = st.icon;
              return (
                <li key={f.sourceId} className="flex items-start gap-3 py-2.5">
                  <Icon className={cn("mt-0.5 size-4 shrink-0", st.cls)} aria-hidden />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-ink" title={f.name}>
                      {f.name}
                    </p>
                    {f.detail && <p className="break-words text-xs text-ink-muted">{f.detail}</p>}
                  </div>
                  <span className="shrink-0 text-xs text-ink-muted">{st.label}</span>
                </li>
              );
            })}
          </ul>
        </SolidPanel>
      )}

      {warnedSources.length > 0 && (
        <SolidPanel className="p-4 sm:p-5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Warnings</h2>
          <ul className="mt-3 space-y-2">
            {warnedSources.map((s) => (
              <li key={s.id} className="text-sm">
                <p className="truncate font-medium text-ink" title={s.originalName ?? s.driveUrl ?? s.id}>
                  {s.originalName ?? s.driveUrl ?? s.id}
                </p>
                <ul className="mt-0.5 list-disc pl-5 text-xs text-ink-muted">
                  {s.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </SolidPanel>
      )}

      {events.length > 0 && (
        <SolidPanel className="p-4 sm:p-5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Activity</h2>
          <ul className="thin-scrollbar mt-3 max-h-72 space-y-1.5 overflow-y-auto pr-1" aria-label="Event log">
            {events.map((e) => (
              <li key={e.id} className="flex gap-3 text-xs">
                <time className="w-16 shrink-0 tabular-nums text-ink-muted" dateTime={e.createdAt} suppressHydrationWarning>
                  {new Date(e.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                </time>
                <span className="min-w-0 break-words text-ink">{e.message}</span>
              </li>
            ))}
          </ul>
        </SolidPanel>
      )}
    </div>
  );
}
