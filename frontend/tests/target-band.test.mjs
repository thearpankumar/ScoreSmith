import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { effectiveTarget, getTargetBand, targetBandThresholds } from "../lib/rag.ts";

const fixture = JSON.parse(readFileSync(new URL("../../backend/tests/data/target_band_cases.json", import.meta.url), "utf8"));

test("every shared parity case maps to the expected band", () => {
  assert.ok(fixture.cases.length > 100);
  for (const c of fixture.cases) {
    assert.equal(getTargetBand(c.score, c.target).key, c.band, `target=${c.target} score=${c.score}`);
  }
});

test("thresholds match the shared fixture for every target", () => {
  for (const [key, expected] of Object.entries(fixture.thresholds)) {
    const target = key === "None" ? null : Number(key);
    const got = Object.fromEntries(targetBandThresholds(target).map((t) => [t.band.key, t.min]));
    for (const [band, min] of Object.entries(expected)) {
      if (min === null) assert.equal(got[band], undefined, `${key}:${band} should be absent`);
      else assert.equal(got[band], min, `${key}:${band}`);
    }
  }
});

test("missing or non-positive targets fall back to 7", () => {
  for (const t of [null, undefined, 0, -3, Number.NaN]) assert.equal(effectiveTarget(t), 7);
  assert.equal(effectiveTarget(4), 4);
});

test("a low target of 4 treats 4.0 as meeting it", () => {
  assert.equal(getTargetBand(4, 4).key, "meets");
  assert.equal(getTargetBand(3.6, 4).key, "near");
  assert.equal(getTargetBand(1.9, 4).key, "critical");
});
