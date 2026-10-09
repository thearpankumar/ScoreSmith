"use client";

import { useState } from "react";
import { ChevronDown, Search, SlidersHorizontal, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DEFAULT_EVAL_QUERY, type EvalQuery, type EvalSort } from "@/lib/collab-client";
import { cn } from "@/lib/utils";

export const SORT_OPTIONS: ReadonlyArray<{ value: EvalSort; label: string }> = [
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "score_desc", label: "Score: high to low" },
  { value: "score_asc", label: "Score: low to high" },
  { value: "name_asc", label: "Name: A to Z" },
  { value: "name_desc", label: "Name: Z to A" },
];

const STATUS: ReadonlyArray<[EvalQuery["status"], string]> = [
  ["all", "All statuses"],
  ["active", "In progress"],
  ["completed", "Completed"],
  ["failed", "Failed"],
];

const BANDS: ReadonlyArray<[string, string]> = [
  ["band_10_9", "Excellent (9-10)"],
  ["band_8", "Good (8)"],
  ["band_7", "Acceptable (7)"],
  ["band_6", "Needs improvement (6)"],
  ["band_5", "Weak (5)"],
  ["band_4", "Poor (4)"],
  ["band_3_0", "Critical (0-3)"],
];

/** Human labels of the active filters, for the summary chips and the Excel "filters" subtitle. */
export function describeQuery(q: EvalQuery, scorecardName: (id: string) => string | null): string[] {
  const out: string[] = [];
  if (q.q.trim()) out.push(`Search: “${q.q.trim()}”`);
  if (q.status !== "all") out.push(`Status: ${STATUS.find(([v]) => v === q.status)?.[1]}`);
  if (q.scorecardId) out.push(`Workflow: ${scorecardName(q.scorecardId) ?? "selected"}`);
  if (q.batchId) out.push("Batch");
  if (q.bands.length) out.push(`Band: ${q.bands.map((b) => BANDS.find(([v]) => v === b)?.[1] ?? b).join(", ")}`);
  if (q.minScore || q.maxScore) out.push(`Score ${q.minScore || "0"}-${q.maxScore || "10"}`);
  if (q.meetsTarget !== "any") out.push(q.meetsTarget === "yes" ? "Meets target" : "Below target");
  if (q.dateFrom || q.dateTo) out.push(`Dates: ${q.dateFrom || "…"} to ${q.dateTo || "…"}`);
  if (q.runner !== "any") out.push(q.runner === "me" ? "Run by me" : "Run by others");
  if (q.shared !== "any") out.push(q.shared === "shared" ? "Shared charts" : "Private charts");
  return out;
}

export function hasFilters(q: EvalQuery): boolean {
  return describeQuery(q, () => null).length > 0;
}

export function EvalFiltersBar({
  query,
  searchText,
  onSearchText,
  onChange,
  scorecards,
  total,
}: {
  query: EvalQuery;
  searchText: string;
  onSearchText: (v: string) => void;
  onChange: (next: EvalQuery) => void;
  scorecards: Array<{ id: string; name: string }>;
  total: number | null;
}) {
  const [more, setMore] = useState(false);
  const set = <K extends keyof EvalQuery>(key: K, value: EvalQuery[K]) => onChange({ ...query, [key]: value });
  const active = hasFilters(query) || searchText.trim() !== "";
  const advancedCount = [
    query.bands.length > 0,
    query.minScore !== "" || query.maxScore !== "",
    query.meetsTarget !== "any",
    query.dateFrom !== "" || query.dateTo !== "",
    query.runner !== "any",
    query.shared !== "any",
  ].filter(Boolean).length;

  return (
    <div className="flex flex-col gap-2" role="group" aria-label="Filter and sort evaluations">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-44 flex-1 basis-56">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-muted" aria-hidden />
          <input
            type="search"
            value={searchText}
            onChange={(e) => onSearchText(e.target.value)}
            placeholder="Search name, person, email, workflow…"
            aria-label="Search evaluations"
            className="h-11 w-full glass-field rounded-lg pl-9 pr-3 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
          />
        </div>
        <Sel label="Status" value={query.status} onChange={(v) => set("status", v as EvalQuery["status"])} options={STATUS.map(([v, l]) => [v, l])} />
        <Sel
          label="Workflow (scorecard)"
          value={query.scorecardId ?? ""}
          onChange={(v) => set("scorecardId", v || null)}
          options={[["", "All workflows"], ...scorecards.map((s) => [s.id, s.name] as [string, string])]}
          wide
        />
        <Sel label="Sort" value={query.sort} onChange={(v) => set("sort", v as EvalSort)} options={SORT_OPTIONS.map((o) => [o.value, o.label])} />
        <Button type="button" variant="outline" size="sm" className="min-h-11" aria-expanded={more} aria-controls="more-filters" onClick={() => setMore((m) => !m)}>
          <SlidersHorizontal aria-hidden /> More filters{advancedCount > 0 ? ` (${advancedCount})` : ""}
          <ChevronDown className={cn("transition-transform motion-reduce:transition-none", more && "rotate-180")} aria-hidden />
        </Button>
      </div>

      {more && (
        <div id="more-filters" className="grid grid-cols-1 gap-2 rounded-xl border border-hairline bg-white/50 p-3 sm:grid-cols-2 lg:grid-cols-3">
          <Sel
            label="Score band"
            value={query.bands[0] ?? ""}
            onChange={(v) => set("bands", v ? [v] : [])}
            options={[["", "Any score band"], ...BANDS]}
            block
          />
          <Sel
            label="Against target"
            value={query.meetsTarget}
            onChange={(v) => set("meetsTarget", v as EvalQuery["meetsTarget"])}
            options={[["any", "Any result vs target"], ["yes", "Meets or beats target"], ["no", "Below target"]]}
            block
          />
          <div className="flex items-center gap-2">
            <NumberField label="Min score" value={query.minScore} onChange={(v) => set("minScore", v)} />
            <span aria-hidden className="text-ink-muted">to</span>
            <NumberField label="Max score" value={query.maxScore} onChange={(v) => set("maxScore", v)} />
          </div>
          <div className="flex items-center gap-2">
            <DateField label="From date" value={query.dateFrom} onChange={(v) => set("dateFrom", v)} />
            <span aria-hidden className="text-ink-muted">to</span>
            <DateField label="To date" value={query.dateTo} onChange={(v) => set("dateTo", v)} />
          </div>
          <Sel label="Run by" value={query.runner} onChange={(v) => set("runner", v as EvalQuery["runner"])} options={[["any", "Run by anyone"], ["me", "Run by me"], ["others", "Run by collaborators"]]} block />
          <Sel label="Charts" value={query.shared} onChange={(v) => set("shared", v as EvalQuery["shared"])} options={[["any", "Shared and private charts"], ["shared", "Shared charts only"], ["private", "Private charts only"]]} block />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted" aria-live="polite">
        {total !== null && (
          <span className="tabular-nums" data-testid="eval-total">
            {total.toLocaleString()} matching evaluation{total === 1 ? "" : "s"}
          </span>
        )}
        {active && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="min-h-11 sm:min-h-8"
            onClick={() => {
              onSearchText("");
              onChange({ ...DEFAULT_EVAL_QUERY, sort: query.sort, batchId: query.batchId });
            }}
          >
            <X aria-hidden /> Clear filters
          </Button>
        )}
      </div>
    </div>
  );
}

function Sel({
  label,
  value,
  onChange,
  options,
  wide,
  block,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: ReadonlyArray<readonly [string, string]> | Array<[string, string]>;
  wide?: boolean;
  block?: boolean;
}) {
  return (
    <label className={cn("flex items-center", block && "w-full")}>
      <span className="sr-only">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className={cn(
          "h-11 glass-field rounded-lg px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
          wide ? "max-w-[16rem]" : "max-w-[13rem]",
          block && "w-full max-w-none",
        )}
      >
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
    </label>
  );
}

function NumberField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <input
      type="number"
      inputMode="decimal"
      min={0}
      max={10}
      step={0.1}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      placeholder={label}
      className="h-11 w-full min-w-0 glass-field rounded-lg px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
    />
  );
}

function DateField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <input
      type="date"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      className="h-11 w-full min-w-0 glass-field rounded-lg px-2 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
    />
  );
}
