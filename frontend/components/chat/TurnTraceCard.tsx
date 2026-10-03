import { memo, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Sparkles, X } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ChatTurnEvent } from "@/lib/types";

/**
 * Replaces the old generic "Assistant is thinking…" bouncing-dots indicator with a real,
 * granular, LIVE trace of what the AI pipeline is actually doing — including, when the
 * multi-agent research fan-out (`research_kpis` in `backend/app/ai/scorecard_builder.py`)
 * spawns up to `MAX_CATEGORIES` concurrent research agents (one per decided KPI category),
 * what EACH one is doing individually (its own searches, result counts, synthesis) — not
 * just one shared "thinking" line for the whole turn.
 *
 * Layout: a "Master" section for the orchestrator (`research_kpis`'s category-deciding step,
 * `propose_kpis`'s consolidate/propose/confirm steps), plus one card per concurrently
 * running research agent, laid out in a grid so it visually reads as "several things
 * happening at once", not a single sequential list — the exact problem the old indicator
 * had. Each actor's card shows its own recent steps (not just the latest message), so a
 * research agent's "searching -> found N results -> synthesizing -> done" progression is
 * visible as it happens, matching the level of granularity actual live-agent-trace UIs
 * (Claude Code / Cursor / LangGraph Studio's run view) use: coarse enough to read at a
 * glance, granular enough to show real per-agent activity.
 *
 * `active` (whether the OVERALL turn is still running — `pending || turnInProgress` in
 * `ChatWorkspace`) controls whether a still-in-progress actor shows a spinner; when false
 * (e.g. rendering a just-finished turn's trace momentarily before it's cleared), every
 * actor renders in its finished state instead.
 *
 * **Multi-round research** (see `MAX_RESEARCH_ROUNDS`/`_assess_research_coverage` in
 * `backend/app/ai/scorecard_builder.py`): `research_kpis` can now run more than one
 * bounded round of the fan-out when the master isn't yet confident coverage is sufficient.
 * Every event carries a `round` (default 1 — see `ChatTurnEvent`'s own docstring). The
 * COMMON case (a single round, the overwhelming majority of turns) renders exactly as
 * before — no "Round" header — so there's no visual regression for it. Only once a SECOND
 * round genuinely appears does this render one "Round N" section per round (each with its
 * own Master trace + per-agent grid, reusing the same `ActorTrace` component/visual
 * pattern below unchanged), oldest first, so a multi-round run reads top-to-bottom as
 * "what happened, round by round" instead of interleaving two rounds' agents together.
 * Only the LATEST round is ever shown as still "live" (spinner-eligible) — every earlier
 * round necessarily already finished before the next one's angles were even decided.
 */
export const TurnTraceCard = memo(function TurnTraceCard({
  events,
  active,
  startedAt,
  onCancel,
  cancelling,
}: {
  events: ChatTurnEvent[];
  active: boolean;
  /** Epoch ms the running turn started — shows a live elapsed-time readout while `active`. */
  startedAt?: number | null;
  /** Stops the running turn (POST /cancel); the button is hidden when omitted. */
  onCancel?: () => void;
  cancelling?: boolean;
}) {
  if (events.length === 0) {
    // No events have landed yet — e.g. the very first instant of a turn, before the
    // orchestrator's first write reaches the DB, or (for a brand-new chat's very first
    // message) before the events poll's first tick has landed at all, or before the
    // backend has even committed the session row the client optimistically adopted (see
    // ChatWorkspace.runAssistantTurn). Fall back to a minimal live indicator rather than
    // rendering nothing.
    return (
      <div className="ml-9 flex items-center gap-2 text-xs text-ink-muted" role="status" aria-live="polite">
        <Loader2 className="size-3.5 animate-spin text-lemon-ink" aria-hidden />
        <span>Assistant is getting started…</span>
        {active && <ElapsedTime startedAt={startedAt} />}
        {active && onCancel && <CancelButton onCancel={onCancel} cancelling={cancelling} />}
      </div>
    );
  }

  const rounds = Array.from(new Set(events.map((e) => e.round ?? 1))).sort((a, b) => a - b);
  const maxRound = rounds[rounds.length - 1];
  const showRoundHeaders = rounds.length > 1;

  return (
    <GlassCard
      elevation={2}
      role="status"
      aria-live="polite"
      className="ml-9 max-w-[85%] space-y-3 p-4"
    >
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
        <Sparkles className="size-3.5 text-lemon-ink" aria-hidden />
        {active ? "Working on it" : "Last turn's activity"}
        {active && <ElapsedTime startedAt={startedAt} />}
        {active && onCancel && (
          <span className="ml-auto normal-case tracking-normal">
            <CancelButton onCancel={onCancel} cancelling={cancelling} />
          </span>
        )}
      </div>

      {rounds.map((round) => (
        <RoundTrace
          key={round}
          round={round}
          events={events.filter((e) => (e.round ?? 1) === round)}
          // Only the most recent round can still be live — every earlier round already
          // ran to completion before the next round's angles were decided.
          active={active && round === maxRound}
          showHeader={showRoundHeaders}
        />
      ))}
    </GlassCard>
  );
});

/** "· 1m 05s" — ticks once a second, only while mounted (the card unmounts when the turn ends). */
function ElapsedTime({ startedAt }: { startedAt?: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!startedAt) return null;
  const secs = Math.max(0, Math.floor((now - startedAt) / 1000));
  const label = secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s`;
  return (
    <span className="font-normal normal-case tracking-normal tabular-nums" aria-label={`Elapsed ${label}`}>
      · {label}
    </span>
  );
}

function CancelButton({ onCancel, cancelling }: { onCancel: () => void; cancelling?: boolean }) {
  return (
    <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={cancelling} className="h-6 px-2 text-xs">
      <X className="size-3" aria-hidden />
      {cancelling ? "Stopping…" : "Cancel"}
    </Button>
  );
}

function RoundTrace({
  round,
  events,
  active,
  showHeader,
}: {
  round: number;
  events: ChatTurnEvent[];
  active: boolean;
  showHeader: boolean;
}) {
  const masterEvents = events.filter((e) => e.actor === "master");
  const agentActors = Array.from(new Set(events.map((e) => e.actor).filter((a) => a !== "master"))).sort(
    (a, b) => agentIndex(a) - agentIndex(b),
  );

  return (
    <div className="space-y-2.5">
      {showHeader && (
        <div className="flex items-center gap-1.5 border-t border-hairline pt-2.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted first:border-t-0 first:pt-0">
          Round {round}
        </div>
      )}

      {masterEvents.length > 0 && (
        <ActorTrace label="Master" events={masterEvents} active={active} />
      )}

      {agentActors.length > 0 && (
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {agentActors.map((actor) => (
            <ActorTrace
              key={actor}
              label={`Research agent ${agentIndex(actor)}`}
              events={events.filter((e) => e.actor === actor)}
              active={active}
              compact
            />
          ))}
        </div>
      )}
    </div>
  );
}

function agentIndex(actor: string): number {
  const n = Number(actor.replace("research_agent_", ""));
  return Number.isFinite(n) ? n : 0;
}

/** Recent event types that mean "this actor is done (for now)" — used to decide whether
 * to show a spinner or a checkmark/warning for its most recent line. */
const TERMINAL_EVENT_TYPES = new Set(["completed", "error"]);

function ActorTrace({
  label,
  events,
  active,
  compact,
}: {
  label: string;
  events: ChatTurnEvent[];
  active: boolean;
  compact?: boolean;
}) {
  // "timing" events ("Phase 'x' took 3.2s.") are bookkeeping: shown as one compact muted line
  // instead of crowding out the live steps. Unknown event types fall through and render their
  // `message` like any other step, so a new backend event type never breaks the card.
  const timings = useMemo(() => events.filter((e) => e.eventType === "timing").map(compactTiming), [events]);
  const steps = useMemo(() => events.filter((e) => e.eventType !== "timing"), [events]);
  if (steps.length === 0) return null;
  const latest = steps[steps.length - 1];
  const progress = parseProgress(latest);
  const finished = TERMINAL_EVENT_TYPES.has(latest.eventType);
  const running = active && !finished;
  const errored = latest.eventType === "error";
  // Keep the visible history short — the point is a live, skimmable trace, not a full
  // transcript. Most recent last (rendered bottom-up like a normal log).
  const visible = steps.slice(-4);

  return (
    <div
      className={cn(
        "rounded-xl border px-3 py-2.5 transition-colors",
        errored
          ? "border-[var(--rag-poor)]/30 bg-[var(--rag-poor)]/5"
          : running
            ? "border-lemon-ink/25 bg-lemon-soft/40"
            : "border-hairline bg-solid/40",
      )}
    >
      <div className="flex items-center gap-1.5">
        {errored ? (
          <AlertTriangle className="size-3.5 shrink-0 text-[var(--rag-poor)]" aria-hidden />
        ) : running ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-lemon-ink" aria-hidden />
        ) : (
          <CheckCircle2 className="size-3.5 shrink-0 text-[var(--rag-excellent)]" aria-hidden />
        )}
        <span className={cn("text-xs font-semibold text-ink", compact && "text-[11px]")}>{label}</span>
      </div>
      <ul className={cn("mt-1.5 space-y-1", compact ? "text-[11px]" : "text-xs")}>
        {visible.map((e, i) => {
          const isLast = i === visible.length - 1;
          return (
            <li
              key={e.id}
              className={cn("leading-snug", isLast ? "font-medium text-ink" : "text-ink-muted/80")}
            >
              {e.message}
            </li>
          );
        })}
      </ul>
      {progress && (
        <div
          className="mt-1.5 h-1 overflow-hidden rounded-full bg-hairline"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={progress.total}
          aria-valuenow={progress.done}
        >
          <div
            className="h-full rounded-full bg-lemon-ink/70 transition-[width]"
            style={{ width: `${Math.min(100, Math.round((progress.done / progress.total) * 100))}%` }}
          />
        </div>
      )}
      {timings.length > 0 && (
        <p className="mt-1.5 text-[10px] leading-snug text-ink-muted/70">{timings.join(" · ")}</p>
      )}
    </div>
  );
}

/** "Wrote guidelines for 12/35" -> {done: 12, total: 35}; null for any other (or malformed) message. */
function parseProgress(e: ChatTurnEvent): { done: number; total: number } | null {
  if (e.eventType !== "guidelines" && e.eventType !== "weighting") return null;
  const m = /(\d+)\s*\/\s*(\d+)/.exec(e.message);
  if (!m) return null;
  const done = Number(m[1]);
  const total = Number(m[2]);
  return total > 0 && done <= total ? { done, total } : null;
}

/** "Phase 'weighting' took 3.2s." -> "weighting 3.2s" (raw message when it doesn't match). */
function compactTiming(e: ChatTurnEvent): string {
  const m = /Phase '([^']+)' took ([\d.]+s)/.exec(e.message);
  return m ? `${m[1]} ${m[2]}` : e.message;
}
