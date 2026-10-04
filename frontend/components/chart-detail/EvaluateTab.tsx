"use client";

import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Bot, Loader2, SlidersHorizontal } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { SolidPanel } from "@/components/design-system/SolidPanel";
import { RagBadge } from "@/components/design-system/RagBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ScorePicker } from "./ScorePicker";
import { AiJudge } from "./ai-judge/AiJudge";
import { createManualEvaluation } from "@/lib/api-client";
import { computeWeightedFinalScore, effectiveLeafWeights, leafKpiNodes } from "@/lib/kpi-tree";
import { getRagBand } from "@/lib/rag";
import { cn } from "@/lib/utils";
import type { KpiNode } from "@/lib/types";

export type EvaluateMode = "manual" | "ai";
type Mode = EvaluateMode;

/**
 * Evaluate tab with two parallel paths that both land on the same evaluation result
 * page:
 *  - "Score manually" (default): a 0-10 anchored score picker + reasoning box per leaf
 *    KPI, with a live weighted final score / RAG band computed client-side using the
 *    exact backend judge math. Persists through the plain evaluation CRUD routes — no
 *    AI / Bedrock involved, so it works in environments without AWS credentials.
 *  - "Ask the AI judge": files / Drive links / spreadsheet queued as background jobs (see ./ai-judge).
 */
export function EvaluateTab({
  scorecardId,
  kpiNodes,
  targetScore,
  mode,
}: {
  scorecardId: string;
  kpiNodes: KpiNode[];
  targetScore?: number;
  /** Controlled by the parent so the mode switch can live on the tab row (see ChartDetailTabs). */
  mode: Mode;
}) {
  const [name, setName] = useState("");

  return (
    <div className="flex flex-col gap-4">
      {mode === "manual" ? (
        <ManualScoring
          scorecardId={scorecardId}
          kpiNodes={kpiNodes}
          targetScore={targetScore}
          name={name}
          setName={setName}
        />
      ) : (
        <AiJudge scorecardId={scorecardId} kpiNodes={kpiNodes} />
      )}
    </div>
  );
}

export function EvaluateModeToggle({ mode, onChange }: { mode: Mode; onChange: (m: Mode) => void }) {
  return (
    <div role="group" aria-label="Evaluation mode" className="glass-elev-1 inline-flex h-11 items-center rounded-full p-1">
      <ModeButton active={mode === "manual"} onClick={() => onChange("manual")} icon={SlidersHorizontal}>
        Score manually
      </ModeButton>
      <ModeButton active={mode === "ai"} onClick={() => onChange("ai")} icon={Bot}>
        Ask the AI judge
      </ModeButton>
    </div>
  );
}

function ModeButton({
  active,
  onClick,
  icon: Icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof Bot;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-full px-4 py-2 text-sm font-medium transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        active ? "bg-lemon font-semibold text-lemon-ink shadow-sm" : "text-ink-muted hover:text-ink",
      )}
    >
      <Icon className="size-4" aria-hidden />
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Manual scoring
// ---------------------------------------------------------------------------

function ManualScoring({
  scorecardId,
  kpiNodes,
  targetScore,
  name,
  setName,
}: {
  scorecardId: string;
  kpiNodes: KpiNode[];
  targetScore?: number;
  name: string;
  setName: (v: string) => void;
}) {
  const router = useRouter();
  const leaves = useMemo(() => leafKpiNodes(kpiNodes), [kpiNodes]);
  const effective = useMemo(() => effectiveLeafWeights(kpiNodes), [kpiNodes]);
  const breadcrumbById = useMemo(() => {
    const byId = new Map(kpiNodes.map((n) => [n.id, n]));
    const out = new Map<string, string>();
    kpiNodes.forEach((n) => {
      const names: string[] = [];
      let cur = n.parentId ? byId.get(n.parentId) : undefined;
      while (cur && names.length < 10) {
        names.unshift(cur.name);
        cur = cur.parentId ? byId.get(cur.parentId) : undefined;
      }
      out.set(n.id, names.join(" › "));
    });
    return out;
  }, [kpiNodes]);

  const [summary, setSummary] = useState("");
  const [scores, setScores] = useState<Record<string, number>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scoredCount = leaves.filter((l) => scores[l.id] !== undefined).length;
  const allScored = leaves.length > 0 && scoredCount === leaves.length;
  // Exactly the backend judge's formula (sum of score x root-to-leaf weight product).
  // Unscored leaves contribute 0, so the running total only reaches its true value once
  // every KPI is scored — shown as "provisional" until then.
  const finalScore = useMemo(() => computeWeightedFinalScore(kpiNodes, scores), [kpiNodes, scores]);
  const scoredWeight = leaves.reduce((s, l) => s + (scores[l.id] !== undefined ? (effective[l.id] ?? 0) : 0), 0);
  const band = getRagBand(finalScore);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !allScored || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const evaluation = await createManualEvaluation({
        scorecardId,
        name: name.trim(),
        inputSummary: summary.trim() || name.trim(),
        scores: leaves.map((l) => ({ kpiNodeId: l.id, score: scores[l.id], reasoning: reasons[l.id] ?? "" })),
        finalWeightedScore: finalScore,
        ragBand: band.key,
      });
      router.push(`/charts/${scorecardId}/evaluations/${evaluation.id}`);
    } catch (err) {
      setSubmitting(false);
      setError(
        `Could not save this evaluation${err instanceof Error && err.message ? ` (${err.message})` : ""}. Please try again.`,
      );
    }
  }

  if (leaves.length === 0) {
    return <SolidPanel className="p-5 text-sm text-ink-muted">This scorecard has no KPIs to score yet.</SolidPanel>;
  }

  return (
    <form onSubmit={handleSubmit} className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="flex min-w-0 flex-col gap-4">
        <GlassCard elevation={1} className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="manual-eval-name">Evaluation name</Label>
            <Input
              id="manual-eval-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Ticket #48213 — refund escalation"
              disabled={submitting}
              required
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="manual-eval-summary">What are you scoring? (optional)</Label>
            <Input
              id="manual-eval-summary"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="Short description or link to the work being rated"
              disabled={submitting}
            />
          </div>
        </GlassCard>

        <ol className="flex flex-col gap-3" aria-label="KPIs to score">
          {leaves.map((kpi, i) => {
            const selected = scores[kpi.id];
            const guideline = selected !== undefined ? kpi.guidelines?.find((g) => g.scoreLevel === selected) : undefined;
            const guideId = `guide-${kpi.id}`;
            const crumb = breadcrumbById.get(kpi.id);
            return (
              <li key={kpi.id}>
                <SolidPanel className={cn("p-4", selected !== undefined && "ring-1 ring-[var(--rag-excellent)]/20")}>
                  <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      {crumb && <p className="truncate text-xs text-ink-muted">{crumb}</p>}
                      <p className="font-medium text-ink">
                        <span className="mr-1.5 text-ink-muted tabular-nums">{i + 1}.</span>
                        {kpi.name}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      <Badge variant="muted">L{kpi.level}</Badge>
                      <Badge variant="outline" title="Share of the whole scorecard">
                        {((effective[kpi.id] ?? 0) * 100).toFixed(1)}% of total
                      </Badge>
                      {selected !== undefined && <RagBadge score={selected} size="sm" showScore={false} />}
                    </div>
                  </div>

                  <ScorePicker
                    label={`Score for ${kpi.name}, 0 to 10`}
                    value={selected ?? null}
                    onChange={(v) => setScores((s) => ({ ...s, [kpi.id]: v }))}
                    disabled={submitting}
                    describedBy={guideId}
                  />
                  <div className="mt-1 flex justify-between text-[11px] text-ink-muted" aria-hidden>
                    <span>0 · Critical</span>
                    <span>10 · Excellent</span>
                  </div>

                  <div
                    id={guideId}
                    aria-live="polite"
                    className="mt-3 rounded-lg border border-hairline bg-bg/60 px-3 py-2 text-xs leading-relaxed"
                  >
                    {selected === undefined ? (
                      <span className="text-ink-muted">Pick a level to see its guideline.</span>
                    ) : guideline ? (
                      <>
                        <span className="font-semibold text-ink">Level {selected} guideline: </span>
                        <span className="text-ink">{guideline.qualitativeText}</span>
                        {guideline.quantitativeCriteria && (
                          <span className="ml-1 font-mono text-ink-muted">({guideline.quantitativeCriteria})</span>
                        )}
                      </>
                    ) : (
                      <span className="italic text-[var(--rag-poor)]">No guideline defined for level {selected}.</span>
                    )}
                  </div>

                  <div className="mt-3 flex flex-col gap-1.5">
                    <Label htmlFor={`reason-${kpi.id}`} className="text-xs text-ink-muted">
                      Reasoning (why this score?)
                    </Label>
                    <Textarea
                      id={`reason-${kpi.id}`}
                      value={reasons[kpi.id] ?? ""}
                      onChange={(e) => setReasons((r) => ({ ...r, [kpi.id]: e.target.value }))}
                      placeholder="Evidence or rationale for this score…"
                      className="min-h-16 bg-solid text-sm"
                      disabled={submitting}
                    />
                  </div>
                </SolidPanel>
              </li>
            );
          })}
        </ol>
      </div>

      <SolidPanel className="flex flex-col gap-4 p-5 lg:sticky lg:top-4" aria-live="polite">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">Weighted score</p>
          <p className="mt-1 text-4xl font-semibold tabular-nums text-ink">
            {finalScore.toFixed(2)}
            <span className="text-base font-normal text-ink-muted"> / 10</span>
          </p>
          <div className="mt-2">
            <RagBadge score={finalScore} showScore={false} />
          </div>
          {!allScored && (
            <p className="mt-2 text-xs text-ink-muted">
              Provisional — unscored KPIs count as 0 ({Math.round(scoredWeight * 100)}% of total weight scored).
            </p>
          )}
          {targetScore !== undefined && targetScore > 0 && (
            <p className="mt-2 text-xs text-ink-muted">
              Target {targetScore.toFixed(1)} ·{" "}
              <span className={finalScore >= targetScore ? "text-[var(--rag-excellent)]" : "text-[var(--rag-poor)]"}>
                {finalScore >= targetScore ? "meets target" : `${(targetScore - finalScore).toFixed(2)} below target`}
              </span>
            </p>
          )}
        </div>

        <div>
          <div className="mb-1 flex justify-between text-xs text-ink-muted">
            <span>KPIs scored</span>
            <span className="tabular-nums">
              {scoredCount}/{leaves.length}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-black/5">
            <div
              className="h-full rounded-full bg-lemon transition-[width]"
              style={{ width: `${(scoredCount / leaves.length) * 100}%` }}
            />
          </div>
        </div>

        {error && (
          <p className="text-sm text-[var(--rag-poor)]" role="alert">
            {error}
          </p>
        )}

        <Button type="submit" disabled={submitting || !allScored || !name.trim()}>
          {submitting ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden /> Saving…
            </>
          ) : (
            "Submit evaluation"
          )}
        </Button>
        {(!allScored || !name.trim()) && (
          <p className="text-xs text-ink-muted">
            {!name.trim() ? "Give this evaluation a name" : ""}
            {!name.trim() && !allScored ? " and score" : !allScored ? "Score" : ""}
            {!allScored ? ` the remaining ${leaves.length - scoredCount} KPI${leaves.length - scoredCount === 1 ? "" : "s"}` : ""}
            {" "}to submit.
          </p>
        )}
      </SolidPanel>
    </form>
  );
}
