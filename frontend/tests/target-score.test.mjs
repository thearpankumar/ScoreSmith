import assert from "node:assert/strict";
import { test } from "node:test";

import { parseTargetInput, targetEditPermission } from "../lib/target-score.ts";

test("accepts 0.1-10 and rounds to two decimals", () => {
  assert.deepEqual(parseTargetInput("7"), { ok: true, value: 7 });
  assert.deepEqual(parseTargetInput(" 4.5 "), { ok: true, value: 4.5 });
  assert.deepEqual(parseTargetInput("6,25"), { ok: true, value: 6.25 });
  assert.deepEqual(parseTargetInput("6.999"), { ok: true, value: 7 });
  assert.deepEqual(parseTargetInput("0.1"), { ok: true, value: 0.1 });
  assert.deepEqual(parseTargetInput("10"), { ok: true, value: 10 });
});

test("rejects empty, non-numeric and out-of-range input", () => {
  for (const bad of ["", "  ", "abc", "-1", "0", "0.05", "10.01", "11", "1e2", "7.", ".5", "NaN"]) {
    assert.equal(parseTargetInput(bad).ok, false, `"${bad}" should be rejected`);
  }
  assert.match(parseTargetInput("11").error, /between 0\.1 and 10/);
});

test("target stays editable on draft and published, locked for archived and non-owners", () => {
  const sc = (status) => ({ status, ownerId: "u1", ownerName: "Ada" });
  assert.equal(targetEditPermission(sc("draft"), "u1").canEdit, true);
  assert.equal(targetEditPermission(sc("published"), "u1").canEdit, true);
  assert.equal(targetEditPermission(sc("archived"), "u1").canEdit, false);
  assert.equal(targetEditPermission(sc("draft"), "u2").canEdit, false);
  assert.match(targetEditPermission(sc("draft"), "u2").lockedReason, /Ada/);
  assert.equal(targetEditPermission(sc("published"), null).canEdit, true);
});
