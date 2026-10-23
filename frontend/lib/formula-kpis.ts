/**
 * The KPI list in the formula builder: which KPIs a formula can reference (the scorecard's leaf KPIs,
 * in scorecard order), how each one is badged, and the search / filter / sort applied to the list.
 * Works for both a saved scorecard's KpiNodes and a chat draft's DraftKpis (same id/parentId/weight
 * shape). Pure and dependency-free so `node --test` can import it directly.
 */

export interface FormulaKpi {
  name: string;
  /** Names of the categories above this KPI, outermost first (empty for a top-level KPI). */
  path: string[];
  /** Share of the default weighted average in percent; 0 when the KPI is excluded from scoring. */
  weight: number | null;
  /** Position in the scorecard (depth-first tree order). */
  order: number;
}

interface KpiLike {
  id: string;
  name: string;
  parentId: string | null;
  weight: number | null;
  includedInScoring?: boolean;
  displayOrder?: number;
}

/** Leaf KPIs in tree order. A name that appears twice is listed once: formulas reference KPIs by name. */
export function formulaKpis(nodes: KpiLike[]): FormulaKpi[] {
  const ids = new Set(nodes.map((n) => n.id));
  const position = new Map(nodes.map((n, i) => [n.id, i]));
  const children = new Map<string | null, KpiLike[]>();
  nodes.forEach((n) => {
    const key = n.parentId !== null && ids.has(n.parentId) ? n.parentId : null;
    children.set(key, [...(children.get(key) ?? []), n]);
  });
  const rank = (n: KpiLike) => [n.displayOrder ?? 0, position.get(n.id) ?? 0];
  children.forEach((list) => list.sort((a, b) => rank(a)[0] - rank(b)[0] || rank(a)[1] - rank(b)[1]));

  const out: FormulaKpi[] = [];
  const seen = new Set<string>();
  const visit = (n: KpiLike, path: string[]) => {
    const kids = children.get(n.id);
    if (kids?.length) {
      kids.forEach((k) => visit(k, [...path, n.name]));
      return;
    }
    if (seen.has(n.name)) return;
    seen.add(n.name);
    out.push({ name: n.name, path, weight: n.includedInScoring === false ? 0 : n.weight, order: out.length });
  };
  (children.get(null) ?? []).forEach((n) => visit(n, []));
  return out;
}

const CODE_RE = /^\s*([A-Za-z]{1,4})[-\s.]?(\d{1,3})\b/;

/** A short badge for a KPI: its code when the name starts with one ("LD-6: …" -> "LD6"), otherwise the
 * initials of its first two words ("Complaint Rate" -> "CR"). */
export function kpiMonogram(name: string): string {
  const code = CODE_RE.exec(name);
  if (code) return (code[1].slice(0, 2) + code[2]).toUpperCase();
  const words: string[] = name.match(/[\p{L}\p{N}]+/gu) ?? [];
  if (words.length === 0) return "#";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/** Tint pairs (100-level background, 800-level text): every pair is well above 4.5:1 contrast. */
export const KPI_TINTS = [
  { bg: "#dbeafe", fg: "#1e40af" },
  { bg: "#dcfce7", fg: "#166534" },
  { bg: "#ede9fe", fg: "#5b21b6" },
  { bg: "#fef3c7", fg: "#92400e" },
  { bg: "#ffe4e6", fg: "#9f1239" },
  { bg: "#ccfbf1", fg: "#115e59" },
  { bg: "#ffedd5", fg: "#9a3412" },
  { bg: "#e0f2fe", fg: "#075985" },
  { bg: "#fae8ff", fg: "#86198f" },
  { bg: "#e2e8f0", fg: "#334155" },
] as const;

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}

/** KPIs of one family share a colour: same category, else the same code prefix ("LD-…"). */
export function kpiTint(kpi: Pick<FormulaKpi, "name" | "path">) {
  const family = kpi.path[kpi.path.length - 1] ?? CODE_RE.exec(kpi.name)?.[1].toUpperCase() ?? kpi.name;
  return KPI_TINTS[hash(family) % KPI_TINTS.length];
}

export type KpiFilter = "all" | "used" | "unused";
export type KpiSort = "order" | "az" | "za" | "used" | "category";

export const KPI_SORTS: { id: KpiSort; label: string }[] = [
  { id: "order", label: "Scorecard order" },
  { id: "az", label: "Name A → Z" },
  { id: "za", label: "Name Z → A" },
  { id: "used", label: "Most used" },
  { id: "category", label: "Category" },
];

const terms = (query: string) => query.toLowerCase().split(/\s+/).filter(Boolean);
const byName = (a: FormulaKpi, b: FormulaKpi) =>
  a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });

/** The KPIs to list: every search term must appear in the name or a category name. */
export function visibleKpis(
  kpis: FormulaKpi[],
  view: { query: string; filter: KpiFilter; sort: KpiSort },
  usage: Map<string, number>,
): FormulaKpi[] {
  const words = terms(view.query);
  const list = kpis.filter((k) => {
    const used = (usage.get(k.name) ?? 0) > 0;
    if (view.filter === "used" && !used) return false;
    if (view.filter === "unused" && used) return false;
    const haystack = `${k.name} ${k.path.join(" ")}`.toLowerCase();
    return words.every((w) => haystack.includes(w));
  });
  const sorted = list.slice();
  if (view.sort === "az") sorted.sort(byName);
  else if (view.sort === "za") sorted.sort((a, b) => byName(b, a));
  else if (view.sort === "used")
    sorted.sort((a, b) => (usage.get(b.name) ?? 0) - (usage.get(a.name) ?? 0) || a.order - b.order);
  else if (view.sort === "category") {
    sorted.sort(
      (a, b) => a.path.join(" / ").localeCompare(b.path.join(" / "), undefined, { numeric: true }) || a.order - b.order,
    );
  } else sorted.sort((a, b) => a.order - b.order);
  return sorted;
}

/** Splits `text` into plain and matching parts for highlighting the search terms. */
export function highlight(text: string, query: string): { text: string; hit: boolean }[] {
  const lower = text.toLowerCase();
  const ranges: [number, number][] = [];
  terms(query).forEach((w) => {
    for (let at = lower.indexOf(w); at !== -1; at = lower.indexOf(w, at + w.length)) ranges.push([at, at + w.length]);
  });
  if (ranges.length === 0) return [{ text, hit: false }];
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  ranges.forEach(([s, e]) => {
    const last = merged[merged.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  });
  const parts: { text: string; hit: boolean }[] = [];
  let pos = 0;
  merged.forEach(([s, e]) => {
    if (s > pos) parts.push({ text: text.slice(pos, s), hit: false });
    parts.push({ text: text.slice(s, e), hit: true });
    pos = e;
  });
  if (pos < text.length) parts.push({ text: text.slice(pos), hit: false });
  return parts;
}
