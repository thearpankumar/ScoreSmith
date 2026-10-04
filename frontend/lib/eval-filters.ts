// Pure filtering for the Evaluations list. A "workflow" is the scorecard an evaluation was run against, so the
// primary filter is by scorecard; the status filter groups the pipeline states into what a user asks about
// ("what is still running?", "what failed?"). Kept free of React so node:test can load it directly.
import type { Evaluation } from "./types";

export const ALL = "all";

export type StatusFilter = "all" | "active" | "completed" | "failed";

export interface EvaluationFilters {
  /** A scorecard id, or `ALL`. */
  scorecardId: string;
  status: StatusFilter;
}

export const NO_FILTERS: EvaluationFilters = { scorecardId: ALL, status: ALL };

export const STATUS_OPTIONS: ReadonlyArray<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "In progress" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

export function parseStatusFilter(value: string | undefined | null): StatusFilter {
  return value === "active" || value === "completed" || value === "failed" ? value : ALL;
}

function matchesStatus(evaluation: Pick<Evaluation, "status">, status: StatusFilter): boolean {
  if (status === ALL) return true;
  // "In progress" is everything that has not finished (queued, fetching, scoring, and the legacy pending states).
  if (status === "active") return evaluation.status !== "completed" && evaluation.status !== "failed";
  return evaluation.status === status;
}

export function filterEvaluations<T extends Pick<Evaluation, "scorecardId" | "status">>(
  rows: T[],
  filters: EvaluationFilters,
): T[] {
  return rows.filter(
    (e) => (filters.scorecardId === ALL || e.scorecardId === filters.scorecardId) && matchesStatus(e, filters.status),
  );
}

export interface ScorecardOption {
  id: string;
  name: string;
  count: number;
}

/** The scorecards that have at least one evaluation, A-Z, each with how many it has. */
export function scorecardOptions(rows: Array<Pick<Evaluation, "scorecardId" | "scorecardName">>): ScorecardOption[] {
  const byId = new Map<string, ScorecardOption>();
  for (const e of rows) {
    const existing = byId.get(e.scorecardId);
    if (existing) existing.count += 1;
    else byId.set(e.scorecardId, { id: e.scorecardId, name: e.scorecardName || "Untitled scorecard", count: 1 });
  }
  return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/** A scorecard filter that no longer matches any evaluation (the scorecard was deleted) falls back to "all". */
export function sanitizeScorecardFilter(scorecardId: string, options: ScorecardOption[]): string {
  return scorecardId === ALL || options.some((o) => o.id === scorecardId) ? scorecardId : ALL;
}

export function hasActiveFilters(filters: EvaluationFilters): boolean {
  return filters.scorecardId !== ALL || filters.status !== ALL;
}
