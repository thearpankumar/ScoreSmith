// Pure selection logic for the Evaluations list multi-select / Excel export. Kept free of React so node:test can
// load it directly. The selection is an explicit set of evaluation ids; the whole list is one page (the list
// endpoint is capped at 200), so "all matching the filter" is simply every selectable visible row.
import type { Evaluation } from "./types";

/** Server-side export cap; equals the list fetch cap, so a select-all can never exceed it. */
export const MAX_EXPORT = 200;

export type HeaderState = "none" | "some" | "all";

/** Finished (completed or failed) evaluations can be selected: they can be deleted safely. In-flight rows cannot. */
export function isSelectable(e: Pick<Evaluation, "status">): boolean {
  return e.status === "completed" || e.status === "failed";
}

/** Only completed evaluations that carry a real score can go into the Excel export. */
export function isExportable(e: Pick<Evaluation, "status" | "finalWeightedScore">): boolean {
  return e.status === "completed" && typeof e.finalWeightedScore === "number" && Number.isFinite(e.finalWeightedScore);
}

export function selectableIds(rows: Array<Pick<Evaluation, "id" | "status">>): string[] {
  return rows.filter(isSelectable).map((e) => e.id);
}

/** The exportable subset of a selection, in list order, capped at `MAX_EXPORT`. */
export function exportableSelection(
  selected: ReadonlySet<string>,
  rows: Array<Pick<Evaluation, "id" | "status" | "finalWeightedScore">>,
): string[] {
  return rows.filter((e) => selected.has(e.id) && isExportable(e)).map((e) => e.id).slice(0, MAX_EXPORT);
}

export function toggle(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

export function selectMany(selected: ReadonlySet<string>, ids: readonly string[]): Set<string> {
  return new Set([...selected, ...ids]);
}

export function deselectMany(selected: ReadonlySet<string>, ids: readonly string[]): Set<string> {
  const drop = new Set(ids);
  return new Set([...selected].filter((id) => !drop.has(id)));
}

/**
 * Shift-click range: every id between `anchor` and `target` (inclusive) in the visible order is set to
 * `select`. Falls back to the target alone when the anchor is missing / not visible.
 */
export function rangeSelect(
  selected: ReadonlySet<string>,
  visibleSelectable: readonly string[],
  anchor: string | null,
  target: string,
  select: boolean,
): Set<string> {
  const a = anchor === null ? -1 : visibleSelectable.indexOf(anchor);
  const b = visibleSelectable.indexOf(target);
  if (b === -1) return new Set(selected);
  const [from, to] = a === -1 ? [b, b] : [Math.min(a, b), Math.max(a, b)];
  const ids = visibleSelectable.slice(from, to + 1);
  return select ? selectMany(selected, ids) : deselectMany(selected, ids);
}

/** Drops ids that no longer exist or are no longer selectable (list refresh, delete, status change). */
export function prune(selected: ReadonlySet<string>, selectableNow: readonly string[]): Set<string> {
  const keep = new Set(selectableNow);
  const next = new Set([...selected].filter((id) => keep.has(id)));
  return next.size === selected.size ? (selected as Set<string>) : next;
}

export function headerState(visibleSelectable: readonly string[], selected: ReadonlySet<string>): HeaderState {
  if (visibleSelectable.length === 0) return "none";
  const n = visibleSelectable.filter((id) => selected.has(id)).length;
  return n === 0 ? "none" : n === visibleSelectable.length ? "all" : "some";
}

export function counts(selected: ReadonlySet<string>, visibleSelectable: readonly string[]) {
  const visible = new Set(visibleSelectable);
  const total = selected.size;
  const inView = [...selected].filter((id) => visible.has(id)).length;
  return { total, inView, hidden: total - inView };
}

/** Selected ids in list order (stable, deterministic order), capped at `MAX_EXPORT`. */
export function orderedSelection(selected: ReadonlySet<string>, allOrdered: readonly string[]): string[] {
  return allOrdered.filter((id) => selected.has(id)).slice(0, MAX_EXPORT);
}

/** Human summary of the active filters, shown in the workbook's Summary subtitle. */
export function describeFilters(parts: { workflow?: string | null; status?: string | null }): string | undefined {
  const bits: string[] = [];
  if (parts.workflow) bits.push(`Workflow: ${parts.workflow}`);
  if (parts.status) bits.push(`Status: ${parts.status}`);
  return bits.length ? bits.join(" · ").slice(0, 300) : undefined;
}

/** Selected ids in list order with no cap (bulk delete applies to the whole selection). */
export function selectionInOrder(selected: ReadonlySet<string>, allOrdered: readonly string[]): string[] {
  return allOrdered.filter((id) => selected.has(id));
}

/** Names for the delete confirmation: the first `max` names plus how many more there are. */
export function previewNames(names: readonly string[], max = 5): { shown: string[]; more: number } {
  return { shown: names.slice(0, max), more: Math.max(0, names.length - max) };
}
