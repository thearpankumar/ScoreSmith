// Pure helpers for the chart Trash: row selection (built on the Evaluations list's selection model in eval-bulk.ts,
// explicit-ids mode only - the trash is small and fully loaded), the "N days left" label and the wording of the
// delete confirmation on a chart card. No React, so node:test can load it.

import {
  EMPTY_SELECTION,
  headerState,
  isSelected,
  pruneIds,
  setRows,
  toggleRow,
  type BulkSelection,
  type HeaderState,
} from "./eval-bulk";

export { EMPTY_SELECTION, headerState, isSelected, pruneIds, setRows, toggleRow };
export type { BulkSelection, HeaderState };

/** Click on a row's checkbox: a plain click toggles it; shift+click applies the clicked row's new state to the whole
 *  range between the anchor (the previously clicked row) and it. Falls back to a plain toggle without a valid anchor. */
export function clickRow(
  sel: BulkSelection,
  orderedIds: readonly string[],
  id: string,
  shift: boolean,
  anchor: string | null,
): BulkSelection {
  if (shift && anchor) {
    const a = orderedIds.indexOf(anchor);
    const b = orderedIds.indexOf(id);
    if (a !== -1 && b !== -1) {
      const [from, to] = [Math.min(a, b), Math.max(a, b)];
      return setRows(sel, orderedIds.slice(from, to + 1), !isSelected(sel, id));
    }
  }
  return toggleRow(sel, id);
}

/** Header checkbox: selects every row unless all are already selected, in which case it clears them. */
export function toggleAll(sel: BulkSelection, orderedIds: readonly string[]): BulkSelection {
  return setRows(sel, orderedIds, headerState(sel, orderedIds) !== "all");
}

/** The selected ids in list order (what the restore / purge calls send). */
export function selectedIds(sel: BulkSelection, orderedIds: readonly string[]): string[] {
  return orderedIds.filter((id) => sel.ids.has(id));
}

export function daysLeftLabel(daysLeft: number): string {
  if (daysLeft <= 0) return "Deletes today";
  return daysLeft === 1 ? "1 day left" : `${daysLeft} days left`;
}

/** Calm text for the urgency of an item: the last week is flagged so people notice it. */
export function isUrgent(daysLeft: number): boolean {
  return daysLeft <= 7;
}

export interface DeleteCopy {
  title: string;
  description: string;
  confirmLabel: string;
  /** True when the action is "leave" (only the caller's own access goes). */
  leaves: boolean;
}

/** Wording of the confirm dialog behind a chart card's delete button.
 *  - owner: moves it to the trash (restorable), with an explicit warning when other people collaborate on it;
 *  - editor: removes only their own access. */
export function deleteCopy(args: {
  name: string;
  myRole?: "owner" | "editor";
  collaboratorCount?: number;
  retentionDays?: number;
}): DeleteCopy {
  const days = args.retentionDays ?? 30;
  if (args.myRole === "editor") {
    return {
      title: `Remove “${args.name}” from your charts?`,
      description:
        "This only removes your own access. The owner and the other collaborators keep the chart, its versions and evaluations. You would need a new invitation to get back in.",
      confirmLabel: "Remove for me",
      leaves: true,
    };
  }
  const n = args.collaboratorCount ?? 0;
  const shared =
    n > 0
      ? ` This chart is shared with ${n} ${n === 1 ? "collaborator" : "collaborators"}: it disappears for everybody until you restore it, and any running evaluations on it are cancelled.`
      : " Any running evaluations on it are cancelled.";
  return {
    title: `Move “${args.name}” to the trash?`,
    description: `It stays in the trash for ${days} days and can be restored from there; after that it is deleted permanently, together with its versions and evaluations.${shared}`,
    confirmLabel: "Move to trash",
    leaves: false,
  };
}

/** "3 charts" / "1 chart" - used by the selection bar and the confirmations. */
export function chartsLabel(n: number): string {
  return `${n} ${n === 1 ? "chart" : "charts"}`;
}

/** Up to `max` names for a confirmation list, plus how many more there are. */
export function previewNames(names: readonly string[], max = 5): { shown: string[]; more: number } {
  return { shown: names.slice(0, max), more: Math.max(0, names.length - max) };
}
