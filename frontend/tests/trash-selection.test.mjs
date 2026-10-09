// Selection, labels and dialog wording of the chart Trash (lib/trash-selection.ts).
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

// lib/trash-selection.ts imports ./eval-bulk without an extension (like the rest of the TypeScript sources).
register("./fixtures/alias-loader.mjs", import.meta.url);

const {
  chartsLabel,
  clickRow,
  daysLeftLabel,
  deleteCopy,
  EMPTY_SELECTION,
  headerState,
  isSelected,
  isUrgent,
  previewNames,
  selectedIds,
  toggleAll,
} = await import("../lib/trash-selection.ts");

const ids = ["a", "b", "c", "d", "e"];

test("a plain click toggles one row", () => {
  let sel = clickRow(EMPTY_SELECTION, ids, "b", false, null);
  assert.deepEqual(selectedIds(sel, ids), ["b"]);
  sel = clickRow(sel, ids, "b", false, "b");
  assert.deepEqual(selectedIds(sel, ids), []);
});

test("shift+click selects the whole range between the anchor and the clicked row, in either direction", () => {
  let sel = clickRow(EMPTY_SELECTION, ids, "b", false, null);
  sel = clickRow(sel, ids, "d", true, "b");
  assert.deepEqual(selectedIds(sel, ids), ["b", "c", "d"]);
  let up = clickRow(EMPTY_SELECTION, ids, "d", false, null);
  up = clickRow(up, ids, "a", true, "d");
  assert.deepEqual(selectedIds(up, ids), ["a", "b", "c", "d"]);
});

test("shift+click on a selected row clears the range; without a valid anchor it is a plain toggle", () => {
  let sel = toggleAll(EMPTY_SELECTION, ids);
  sel = clickRow(sel, ids, "d", true, "b");
  assert.deepEqual(selectedIds(sel, ids), ["a", "e"]);
  const fallback = clickRow(EMPTY_SELECTION, ids, "c", true, "gone");
  assert.deepEqual(selectedIds(fallback, ids), ["c"]);
  assert.deepEqual(selectedIds(clickRow(EMPTY_SELECTION, ids, "c", true, null), ids), ["c"]);
});

test("the header checkbox selects all, reports a partial selection, and clears when all are selected", () => {
  assert.equal(headerState(EMPTY_SELECTION, ids), "none");
  const some = clickRow(EMPTY_SELECTION, ids, "a", false, null);
  assert.equal(headerState(some, ids), "some");
  const all = toggleAll(some, ids); // partial -> everything
  assert.equal(headerState(all, ids), "all");
  assert.ok(ids.every((id) => isSelected(all, id)));
  assert.equal(headerState(toggleAll(all, ids), ids), "none");
  assert.equal(headerState(EMPTY_SELECTION, []), "none");
});

test("selectedIds keeps list order and ignores ids that are not listed", () => {
  let sel = clickRow(EMPTY_SELECTION, ids, "d", false, null);
  sel = clickRow(sel, ids, "a", false, "d");
  assert.deepEqual(selectedIds(sel, ids), ["a", "d"]);
  assert.deepEqual(selectedIds(sel, ["d"]), ["d"]);
});

test("labels", () => {
  assert.equal(daysLeftLabel(30), "30 days left");
  assert.equal(daysLeftLabel(1), "1 day left");
  assert.equal(daysLeftLabel(0), "Deletes today");
  assert.equal(isUrgent(7), true);
  assert.equal(isUrgent(8), false);
  assert.equal(chartsLabel(1), "1 chart");
  assert.equal(chartsLabel(3), "3 charts");
  assert.deepEqual(previewNames(["1", "2", "3"], 2), { shown: ["1", "2"], more: 1 });
});

test("an owner's delete means the trash; the dialog warns about collaborators", () => {
  const solo = deleteCopy({ name: "Q3", myRole: "owner", collaboratorCount: 0 });
  assert.equal(solo.leaves, false);
  assert.match(solo.title, /Move “Q3” to the trash/);
  assert.match(solo.description, /30 days/);
  assert.doesNotMatch(solo.description, /everybody/);
  const shared = deleteCopy({ name: "Q3", myRole: "owner", collaboratorCount: 2 });
  assert.match(shared.description, /2 collaborators/);
  assert.match(shared.description, /everybody until you restore it/);
  assert.equal(deleteCopy({ name: "Q3", collaboratorCount: 1 }).confirmLabel, "Move to trash");
});

test("an editor's delete only removes their own access", () => {
  const c = deleteCopy({ name: "Q3", myRole: "editor", collaboratorCount: 3 });
  assert.equal(c.leaves, true);
  assert.match(c.description, /only removes your own access/);
  assert.match(c.description, /owner and the other collaborators keep the chart/);
  assert.equal(c.confirmLabel, "Remove for me");
});
