// Pure-logic tests for the Evaluations list filters (lib/eval-filters.ts). No network, no deps.
import test from "node:test";
import assert from "node:assert/strict";

import {
  ALL,
  NO_FILTERS,
  filterEvaluations,
  hasActiveFilters,
  parseStatusFilter,
  sanitizeScorecardFilter,
  scorecardOptions,
} from "../lib/eval-filters.ts";
import { isActiveStatus } from "../lib/eval-status.ts";

const ev = (id, scorecardId, scorecardName, status) => ({ id, scorecardId, scorecardName, status });
const rows = [
  ev("1", "sc-support", "Customer Support Quality", "completed"),
  ev("2", "sc-support", "Customer Support Quality", "failed"),
  ev("3", "sc-hack", "Hackathon Solution Scoring", "completed"),
  ev("4", "sc-hack", "Hackathon Solution Scoring", "scoring"),
  ev("5", "sc-hack", "Hackathon Solution Scoring", "queued"),
  ev("6", "sc-hack", "Hackathon Solution Scoring", "pending"), // legacy
];

test("no filters returns everything, in the same order", () => {
  assert.deepEqual(filterEvaluations(rows, NO_FILTERS).map((e) => e.id), ["1", "2", "3", "4", "5", "6"]);
  assert.equal(hasActiveFilters(NO_FILTERS), false);
});

test("filters by workflow (scorecard)", () => {
  const out = filterEvaluations(rows, { scorecardId: "sc-hack", status: ALL });
  assert.deepEqual(out.map((e) => e.id), ["3", "4", "5", "6"]);
  assert.equal(hasActiveFilters({ scorecardId: "sc-hack", status: ALL }), true);
});

test("status groups: in progress means not finished (incl. legacy pending), plus completed and failed", () => {
  const f = (status) => filterEvaluations(rows, { scorecardId: ALL, status }).map((e) => e.id);
  assert.deepEqual(f("active"), ["4", "5", "6"]);
  assert.deepEqual(f("completed"), ["1", "3"]);
  assert.deepEqual(f("failed"), ["2"]);
});

test("workflow and status combine", () => {
  assert.deepEqual(filterEvaluations(rows, { scorecardId: "sc-hack", status: "completed" }).map((e) => e.id), ["3"]);
  assert.deepEqual(filterEvaluations(rows, { scorecardId: "sc-support", status: "active" }), []);
});

test("every status the poller treats as active also counts as 'in progress' in the filter", () => {
  for (const status of ["queued", "ingesting", "processing", "scoring"]) {
    assert.equal(isActiveStatus(status), true);
    assert.equal(filterEvaluations([ev("x", "s", "S", status)], { scorecardId: ALL, status: "active" }).length, 1, status);
  }
  for (const status of ["completed", "failed"]) {
    assert.equal(filterEvaluations([ev("x", "s", "S", status)], { scorecardId: ALL, status: "active" }).length, 0, status);
  }
});

test("scorecard options: only scorecards with evaluations, A-Z, with counts", () => {
  assert.deepEqual(scorecardOptions(rows), [
    { id: "sc-support", name: "Customer Support Quality", count: 2 },
    { id: "sc-hack", name: "Hackathon Solution Scoring", count: 4 },
  ].sort((a, b) => a.name.localeCompare(b.name)));
  assert.deepEqual(scorecardOptions([]), []);
  assert.equal(scorecardOptions([ev("1", "s", "", "completed")])[0].name, "Untitled scorecard");
});

test("a filter for a scorecard that has no evaluations any more falls back to all", () => {
  const options = scorecardOptions(rows);
  assert.equal(sanitizeScorecardFilter("sc-hack", options), "sc-hack");
  assert.equal(sanitizeScorecardFilter("deleted-scorecard", options), ALL);
  assert.equal(sanitizeScorecardFilter(ALL, options), ALL);
});

test("status values from the URL are validated", () => {
  assert.equal(parseStatusFilter("failed"), "failed");
  assert.equal(parseStatusFilter("active"), "active");
  assert.equal(parseStatusFilter("bogus"), ALL);
  assert.equal(parseStatusFilter(undefined), ALL);
  assert.equal(parseStatusFilter(null), ALL);
});
