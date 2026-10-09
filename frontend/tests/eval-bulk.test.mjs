// Server-side "select all matching" selection model for the infinite Evaluations list (lib/eval-bulk.ts).
import test from "node:test";
import assert from "node:assert/strict";

import {
  EMPTY_SELECTION,
  exportByFilter,
  exportCap,
  exportableCount,
  headerState,
  hiddenCount,
  isSelectableRow,
  isSelected,
  mergeRows,
  patchRows,
  pruneIds,
  selectAllMatching,
  selectedCount,
  setRows,
  toggleRow,
} from "../lib/eval-bulk.ts";

const row = (id, status = "completed", score = 7) => ({ id, status, finalWeightedScore: status === "completed" ? score : null });

test("explicit mode selects exactly the ticked ids", () => {
  let sel = toggleRow(EMPTY_SELECTION, "a");
  sel = toggleRow(sel, "b");
  assert.deepEqual([...sel.ids].sort(), ["a", "b"]);
  assert.equal(isSelected(sel, "a"), true);
  assert.equal(isSelected(sel, "z"), false);
  assert.equal(selectedCount(sel, 1000), 2);
  sel = toggleRow(sel, "a");
  assert.equal(isSelected(sel, "a"), false);
});

test("select-all-matching is a filter selection: count comes from the server total minus un-ticked rows", () => {
  let sel = selectAllMatching();
  assert.equal(sel.all, true);
  assert.equal(isSelected(sel, "anything-never-loaded"), true); // rows that were never loaded are covered
  assert.equal(selectedCount(sel, 2500), 2500);
  sel = toggleRow(sel, "x");
  sel = toggleRow(sel, "y");
  assert.equal(isSelected(sel, "x"), false);
  assert.equal(selectedCount(sel, 2500), 2498);
  sel = toggleRow(sel, "x"); // un-un-tick
  assert.equal(selectedCount(sel, 2500), 2499);
  assert.equal(exportByFilter(sel), true);
  assert.equal(exportCap(sel), 500);
  assert.equal(exportCap(EMPTY_SELECTION), 200);
});

test("export count in all-mode subtracts un-ticked exportable rows only", () => {
  const rows = [row("a"), row("b"), row("c", "failed"), row("d", "scoring")];
  let sel = selectAllMatching();
  assert.equal(exportableCount(sel, rows, 40), 40);
  sel = toggleRow(sel, "a"); // exportable
  sel = toggleRow(sel, "c"); // failed: not exportable anyway
  assert.equal(exportableCount(sel, rows, 40), 39);
  const explicit = setRows(EMPTY_SELECTION, ["a", "b", "c"], true);
  assert.equal(exportableCount(explicit, rows, 40), 2); // a, b - the failed one is not exportable
});

test("header checkbox state follows the loaded selectable rows", () => {
  const loaded = ["a", "b", "c"];
  assert.equal(headerState(EMPTY_SELECTION, loaded), "none");
  assert.equal(headerState(setRows(EMPTY_SELECTION, ["a"], true), loaded), "some");
  assert.equal(headerState(setRows(EMPTY_SELECTION, loaded, true), loaded), "all");
  assert.equal(headerState(selectAllMatching(), loaded), "all");
  assert.equal(headerState(toggleRow(selectAllMatching(), "b"), loaded), "some");
  assert.equal(headerState(EMPTY_SELECTION, []), "none");
  // clearing loaded rows while in all-mode excludes them
  const cleared = setRows(selectAllMatching(), loaded, false);
  assert.equal(headerState(cleared, loaded), "none");
  assert.equal(selectedCount(cleared, 10), 7);
});

test("only completed or failed rows are selectable", () => {
  assert.equal(isSelectableRow({ status: "completed" }), true);
  assert.equal(isSelectableRow({ status: "failed" }), true);
  for (const s of ["queued", "ingesting", "processing", "scoring", "pending"]) assert.equal(isSelectableRow({ status: s }), false);
});

test("hidden count and pruning of explicit selections", () => {
  const sel = setRows(EMPTY_SELECTION, ["a", "b", "z"], true);
  assert.equal(hiddenCount(sel, ["a", "b"]), 1);
  assert.equal(hiddenCount(selectAllMatching(), []), 0);
  const pruned = pruneIds(sel, new Set(["a", "b"]));
  assert.deepEqual([...pruned.ids].sort(), ["a", "b"]);
  assert.equal(pruneIds(sel, new Set(["a", "b", "z"])), sel); // unchanged -> same object
});

test("merge never duplicates rows; patch replaces in place", () => {
  const a = [{ id: "1", v: 1 }, { id: "2", v: 1 }];
  const merged = mergeRows(a, [{ id: "2", v: 9 }, { id: "3", v: 1 }]);
  assert.deepEqual(merged.map((r) => r.id), ["1", "2", "3"]);
  assert.equal(merged[1].v, 1); // the already-loaded row is kept
  const patched = patchRows(merged, [{ id: "2", v: 5 }, { id: "99", v: 5 }]);
  assert.deepEqual(patched.map((r) => [r.id, r.v]), [["1", 1], ["2", 5], ["3", 1]]); // unknown ids are not appended
});
