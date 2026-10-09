// Selection model for the infinite Evaluations list. Only a window of the matching rows is ever loaded, so
// "select all" cannot be a set of ids: it is a SERVER-SIDE selection - "everything matching the current filter,
// except the rows the user un-ticked" - that the bulk-delete / export endpoints resolve themselves.
// Pure (no React) so node:test can load it.

export interface BulkSelection {
  /** false: exactly `ids`; true: all rows matching the filter that were selectable, minus `excluded`. */
  all: boolean;
  ids: ReadonlySet<string>;
  excluded: ReadonlySet<string>;
}

export const EMPTY_SELECTION: BulkSelection = { all: false, ids: new Set(), excluded: new Set() };

/** Server cap for an export by filter (backend MAX_FILTER_EXPORT) and by explicit ids (MAX_EXPORT_EVALUATIONS). */
export const MAX_FILTER_EXPORT = 500;
export const MAX_IDS_EXPORT = 200;

export interface RowLike {
  id: string;
  status: string;
  finalWeightedScore: number | null;
}

export function isSelectableRow(row: Pick<RowLike, "status">): boolean {
  return row.status === "completed" || row.status === "failed";
}

export function isExportableRow(row: RowLike): boolean {
  return row.status === "completed" && typeof row.finalWeightedScore === "number" && Number.isFinite(row.finalWeightedScore);
}

export function isSelected(sel: BulkSelection, id: string): boolean {
  return sel.all ? !sel.excluded.has(id) : sel.ids.has(id);
}

export function toggleRow(sel: BulkSelection, id: string): BulkSelection {
  if (sel.all) {
    const excluded = new Set(sel.excluded);
    if (excluded.has(id)) excluded.delete(id);
    else excluded.add(id);
    return { ...sel, excluded };
  }
  const ids = new Set(sel.ids);
  if (ids.has(id)) ids.delete(id);
  else ids.add(id);
  return { ...sel, ids };
}

/** Selects (or clears) a set of loaded rows, e.g. the header checkbox for "all loaded". */
export function setRows(sel: BulkSelection, rowIds: readonly string[], selected: boolean): BulkSelection {
  if (sel.all) {
    const excluded = new Set(sel.excluded);
    for (const id of rowIds) {
      if (selected) excluded.delete(id);
      else excluded.add(id);
    }
    return { ...sel, excluded };
  }
  const ids = new Set(sel.ids);
  for (const id of rowIds) {
    if (selected) ids.add(id);
    else ids.delete(id);
  }
  return { ...sel, ids };
}

/** "Select all N matching": switches to the server-side selection of the current filter. */
export function selectAllMatching(): BulkSelection {
  return { all: true, ids: new Set(), excluded: new Set() };
}

/** How many rows the selection covers. `selectableTotal` = rows matching the filter that can be selected (server count). */
export function selectedCount(sel: BulkSelection, selectableTotal: number): number {
  return sel.all ? Math.max(0, selectableTotal - sel.excluded.size) : sel.ids.size;
}

/** How many of them can go into the Excel export (`exportableTotal` = completed-with-score rows matching the filter). */
export function exportableCount(sel: BulkSelection, rows: readonly RowLike[], exportableTotal: number): number {
  if (!sel.all) return rows.filter((r) => sel.ids.has(r.id) && isExportableRow(r)).length;
  const excludedExportable = rows.filter((r) => sel.excluded.has(r.id) && isExportableRow(r)).length;
  return Math.max(0, exportableTotal - excludedExportable);
}

export type HeaderState = "none" | "some" | "all";

/** State of the header checkbox over the LOADED selectable rows. */
export function headerState(sel: BulkSelection, loadedSelectable: readonly string[]): HeaderState {
  if (loadedSelectable.length === 0) return "none";
  const n = loadedSelectable.filter((id) => isSelected(sel, id)).length;
  return n === 0 ? "none" : n === loadedSelectable.length ? "all" : "some";
}

/** Explicit ids that are no longer on screen (a filter change keeps them selected; the bar says so). */
export function hiddenCount(sel: BulkSelection, loadedIds: readonly string[]): number {
  if (sel.all) return 0;
  const loaded = new Set(loadedIds);
  let n = 0;
  for (const id of sel.ids) if (!loaded.has(id)) n++;
  return n;
}

/** Drops explicit ids whose rows vanished (deleted elsewhere) so the count never lies. */
export function pruneIds(sel: BulkSelection, existing: ReadonlySet<string>): BulkSelection {
  if (sel.all) return sel;
  const ids = new Set([...sel.ids].filter((id) => existing.has(id)));
  return ids.size === sel.ids.size ? sel : { ...sel, ids };
}

/** True when the export is resolved by the server from the filter (all-mode); explicit ids are sent as a list. */
export function exportByFilter(sel: BulkSelection): boolean {
  return sel.all;
}

/** The export cap that applies to this selection, for the bar's "limited to N" message. */
export function exportCap(sel: BulkSelection): number {
  return exportByFilter(sel) ? MAX_FILTER_EXPORT : MAX_IDS_EXPORT;
}

/** Merges freshly fetched rows into the list without duplicates; order of existing rows is kept. */
export function mergeRows<T extends { id: string }>(existing: readonly T[], incoming: readonly T[]): T[] {
  const seen = new Set(existing.map((r) => r.id));
  const out = [...existing];
  for (const row of incoming) {
    if (!seen.has(row.id)) {
      seen.add(row.id);
      out.push(row);
    }
  }
  return out;
}

/** Replaces rows in place by id (live progress refresh), dropping nothing and appending nothing. */
export function patchRows<T extends { id: string }>(existing: readonly T[], updates: readonly T[]): T[] {
  const byId = new Map(updates.map((u) => [u.id, u]));
  return existing.map((r) => byId.get(r.id) ?? r);
}
