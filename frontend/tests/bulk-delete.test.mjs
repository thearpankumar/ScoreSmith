// Tests for the bulk delete runner (lib/bulk-delete.ts): concurrency limit, progress, partial failure.
import test from "node:test";
import assert from "node:assert/strict";

import { runBulkDelete, summarizeFailures } from "../lib/bulk-delete.ts";

const tick = () => new Promise((r) => setTimeout(r, 1));

test("deletes every id exactly once and reports progress to the total", async () => {
  const seen = [];
  const progress = [];
  const ids = Array.from({ length: 10 }, (_, i) => `id${i}`);
  const res = await runBulkDelete(ids, async (id) => { seen.push(id); await tick(); }, { onProgress: (d, t) => progress.push([d, t]) });
  assert.deepEqual([...seen].sort(), [...ids].sort());
  assert.equal(res.succeeded.length, 10);
  assert.deepEqual(res.failed, []);
  assert.deepEqual(progress.at(-1), [10, 10]);
  assert.equal(progress.length, 10);
});

test("never runs more than `concurrency` deletes at once", async () => {
  let running = 0;
  let peak = 0;
  await runBulkDelete(Array.from({ length: 12 }, (_, i) => `x${i}`), async () => {
    running += 1;
    peak = Math.max(peak, running);
    await tick();
    running -= 1;
  }, { concurrency: 3 });
  assert.ok(peak <= 3 && peak >= 2, `peak=${peak}`);
});

test("partial failure: successes and failures are separated with reasons; only given ids are touched", async () => {
  const touched = [];
  const res = await runBulkDelete(["a", "b", "c"], async (id) => {
    touched.push(id);
    if (id === "b") throw new Error("Cannot delete: still referenced.");
  });
  assert.deepEqual([...res.succeeded].sort(), ["a", "c"]);
  assert.deepEqual(res.failed, [{ id: "b", message: "Cannot delete: still referenced." }]);
  assert.deepEqual([...touched].sort(), ["a", "b", "c"]);
  assert.match(summarizeFailures(res.failed), /1 evaluation couldn't be deleted: Cannot delete/);
});

test("empty input and non-Error rejections are handled", async () => {
  assert.deepEqual(await runBulkDelete([], async () => {}), { succeeded: [], failed: [] });
  const res = await runBulkDelete(["a"], () => Promise.reject("boom"));
  assert.equal(res.failed[0].message, "Couldn't delete this evaluation.");
});
