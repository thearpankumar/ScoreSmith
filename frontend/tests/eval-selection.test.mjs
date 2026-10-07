// Pure-logic tests for the Evaluations multi-select (lib/eval-selection.ts). No network, no deps.
import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_EXPORT,
  counts,
  deselectMany,
  describeFilters,
  exportableSelection,
  headerState,
  isExportable,
  isSelectable,
  previewNames,
  orderedSelection,
  prune,
  rangeSelect,
  selectMany,
  selectableIds,
  selectionInOrder,
  toggle,
} from "../lib/eval-selection.ts";

const ev = (id, status = "completed", score = 7) => ({ id, status, finalWeightedScore: score });
const rows = [ev("a"), ev("b"), ev("c"), ev("d", "scoring", 0), ev("e", "failed", 0), ev("f")];

test("completed and failed rows are selectable; in-flight rows are not", () => {
  assert.deepEqual(selectableIds(rows), ["a", "b", "c", "e", "f"]);
  assert.equal(isSelectable(ev("x", "queued", 5)), false);
  assert.equal(isSelectable(ev("x", "scoring", 5)), false);
  assert.equal(isSelectable(ev("x", "failed", 0)), true);
});

test("only completed rows with a finite score are exportable", () => {
  assert.equal(isExportable(ev("x")), true);
  assert.equal(isExportable(ev("x", "completed", Number.NaN)), false);
  assert.equal(isExportable(ev("x", "failed", 0)), false);
  assert.equal(isExportable(ev("x", "queued", 5)), false);
});

test("exportableSelection is the scored subset of the selection, in list order", () => {
  const sel = new Set(["f", "e", "a", "d"]); // d is in-flight, e is failed
  assert.deepEqual(exportableSelection(sel, rows), ["a", "f"]);
  assert.deepEqual(exportableSelection(new Set(["e"]), rows), []);
});

test("selectionInOrder keeps the whole selection (incl. failed rows) in list order", () => {
  assert.deepEqual(selectionInOrder(new Set(["f", "e", "a"]), rows.map((r) => r.id)), ["a", "e", "f"]);
});

test("previewNames shows up to max names plus a remainder count", () => {
  assert.deepEqual(previewNames(["a", "b"], 5), { shown: ["a", "b"], more: 0 });
  assert.deepEqual(previewNames(["a", "b", "c", "d", "e", "f", "g"], 5), { shown: ["a", "b", "c", "d", "e"], more: 2 });
});

test("toggle / selectMany / deselectMany are immutable", () => {
  const s = new Set(["a"]);
  assert.deepEqual([...toggle(s, "b")], ["a", "b"]);
  assert.deepEqual([...toggle(s, "a")], []);
  assert.deepEqual([...s], ["a"]);
  assert.deepEqual([...selectMany(s, ["b", "c"])], ["a", "b", "c"]);
  assert.deepEqual([...deselectMany(new Set(["a", "b", "c"]), ["b"])], ["a", "c"]);
});

test("header state transitions none -> some -> all", () => {
  const vis = ["a", "b", "c"];
  assert.equal(headerState(vis, new Set()), "none");
  assert.equal(headerState(vis, new Set(["a"])), "some");
  assert.equal(headerState(vis, new Set(["a", "b", "c", "zzz"])), "all");
  assert.equal(headerState([], new Set(["a"])), "none");
});

test("select-all acts on visible selectable rows only and survives filter changes", () => {
  const visible = ["a", "b"]; // filtered view
  let sel = selectMany(new Set(), visible);
  assert.equal(headerState(visible, sel), "all");
  // Filter changes to a different view: earlier picks stay selected but are counted as hidden.
  const next = ["c", "f"];
  assert.deepEqual(counts(sel, next), { total: 2, inView: 0, hidden: 2 });
  sel = selectMany(sel, ["c"]);
  assert.deepEqual(counts(sel, next), { total: 3, inView: 1, hidden: 2 });
  assert.equal(headerState(next, sel), "some");
});

test("shift-click range selects (or clears) everything between anchor and target", () => {
  const vis = ["a", "b", "c", "f"];
  assert.deepEqual([...rangeSelect(new Set(), vis, "a", "c", true)], ["a", "b", "c"]);
  assert.deepEqual([...rangeSelect(new Set(), vis, "f", "b", true)].sort(), ["b", "c", "f"]);
  assert.deepEqual([...rangeSelect(new Set(vis), vis, "a", "b", false)].sort(), ["c", "f"]);
  // missing anchor falls back to the target alone; unknown target is a no-op
  assert.deepEqual([...rangeSelect(new Set(), vis, null, "b", true)], ["b"]);
  assert.deepEqual([...rangeSelect(new Set(["a"]), vis, "a", "nope", true)], ["a"]);
});

test("prune drops vanished / no-longer-selectable ids and keeps identity when nothing changes", () => {
  const sel = new Set(["a", "b", "gone"]);
  assert.deepEqual([...prune(sel, ["a", "b", "c"])], ["a", "b"]);
  const same = new Set(["a"]);
  assert.equal(prune(same, ["a", "b"]), same);
});

test("orderedSelection follows list order and is capped at MAX_EXPORT", () => {
  assert.deepEqual(orderedSelection(new Set(["c", "a"]), ["a", "b", "c"]), ["a", "c"]);
  const many = Array.from({ length: MAX_EXPORT + 20 }, (_, i) => `id${i}`);
  assert.equal(orderedSelection(new Set(many), many).length, MAX_EXPORT);
});

test("describeFilters builds the workbook subtitle", () => {
  assert.equal(describeFilters({}), undefined);
  assert.equal(describeFilters({ workflow: "Hackathon", status: "Completed" }), "Workflow: Hackathon · Status: Completed");
  assert.equal(describeFilters({ status: "Failed" }), "Status: Failed");
});

test("failed, queued, in-flight and unscored rows never reach the export ids, even when selected", () => {
  const all = [
    ev("ok1"),
    ev("failed1", "failed", 0),
    ev("queued1", "queued", 0),
    ev("scoring1", "scoring", 3),
    ev("nan1", "completed", Number.NaN),
    ev("ok2", "completed", 4.2),
  ];
  const everything = new Set(all.map((r) => r.id));
  assert.deepEqual(exportableSelection(everything, all), ["ok1", "ok2"]);
  assert.deepEqual(exportableSelection(new Set(["failed1", "queued1", "scoring1", "nan1"]), all), []);
});
