"use client";

import { useMemo, useState } from "react";
import { Search } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Input } from "@/components/ui/input";
import { ScorecardCard } from "./ScorecardCard";
import type { Scorecard, ScorecardStatus } from "@/lib/types";

export function ChartsLibraryClient({
  scorecards,
  domains,
  owners,
}: {
  scorecards: Scorecard[];
  domains: string[];
  owners: Array<{ id: string; name: string }>;
}) {
  const [search, setSearch] = useState("");
  const [domain, setDomain] = useState("all");
  const [owner, setOwner] = useState("all");
  const [status, setStatus] = useState<ScorecardStatus | "all">("all");

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return scorecards.filter((s) => {
      if (q && !s.name.toLowerCase().includes(q) && !s.purposeStatement.toLowerCase().includes(q)) return false;
      if (domain !== "all" && s.domain !== domain) return false;
      if (owner !== "all" && s.ownerId !== owner) return false;
      if (status !== "all" && s.status !== status) return false;
      return true;
    });
  }, [scorecards, search, domain, owner, status]);

  return (
    <div className="flex flex-col gap-4">
      <GlassCard elevation={1} className="flex flex-wrap items-center gap-3 p-3">
        <div className="relative min-w-52 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-muted" aria-hidden />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search scorecards…"
            className="bg-solid pl-9"
            aria-label="Search scorecards"
          />
        </div>
        <FilterSelect
          label="Domain"
          value={domain}
          onChange={setDomain}
          options={[{ value: "all", label: "All domains" }, ...domains.map((d) => ({ value: d, label: d }))]}
        />
        <FilterSelect
          label="Owner"
          value={owner}
          onChange={setOwner}
          options={[{ value: "all", label: "All owners" }, ...owners.map((o) => ({ value: o.id, label: o.name }))]}
        />
        <FilterSelect
          label="Status"
          value={status}
          onChange={(v) => setStatus(v as ScorecardStatus | "all")}
          options={[
            { value: "all", label: "All statuses" },
            { value: "published", label: "Published" },
            { value: "draft", label: "Draft" },
            { value: "archived", label: "Archived" },
          ]}
        />
      </GlassCard>

      {filtered.length === 0 ? (
        <p className="py-12 text-center text-sm text-ink-muted">No scorecards match these filters.</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((s) => (
            <ScorecardCard key={s.id} scorecard={s} />
          ))}
        </div>
      )}
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs font-medium text-ink-muted">
      <span className="sr-only">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 rounded-lg border border-hairline bg-solid px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}
