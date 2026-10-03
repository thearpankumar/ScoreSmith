// Run with: npm run test:unit  (node >= 22 strips TypeScript types natively; no extra deps).
import test from "node:test";
import assert from "node:assert/strict";

import {
  draftSignature,
  mergeInterimDraft,
  sameEvents,
  stripEditSummary,
  turnFailureMessage,
  weightedLeafProgress,
} from "../lib/chat-turn-state.ts";

const empty = { sessionId: "s", name: null, domain: null, purposeStatement: null, scope: null, targetScore: null, kpis: [], scoringFormula: null };
const kpi = (name, weight, parentId = null) => ({ id: `draft-${name}`, name, weight, level: 1, parentId, status: "proposed", includedInScoring: true });

test("interim header + KPIs fill an empty preview without any user edit", () => {
  const interim = { ...empty, name: "Sales QA", purposeStatement: "p", targetScore: 8, kpis: [kpi("A", null), kpi("B", null)] };
  const out = mergeInterimDraft(empty, empty, interim);
  assert.equal(out.name, "Sales QA");
  assert.equal(out.targetScore, 8);
  assert.equal(out.kpis.length, 2);
});

test("user-edited fields and KPI list are never clobbered by a poll", () => {
  const server = { ...empty, name: "Old", scope: "s1", kpis: [kpi("A", 50), kpi("B", 50)] };
  const local = { ...server, name: "My name", kpis: [kpi("A", 60), kpi("B", 40)] };
  const interim = { ...server, name: "Server name", scope: "s2", kpis: [kpi("A", 10), kpi("B", 90), kpi("C", 0)] };
  const out = mergeInterimDraft(local, server, interim);
  assert.equal(out.name, "My name"); // edited -> kept
  assert.equal(out.scope, "s2"); // not edited -> interim wins
  assert.deepEqual(out.kpis.map((k) => k.weight), [60, 40]); // KPI list edited -> kept whole
});

test("an empty interim never blanks existing fields; null interim returns local by reference", () => {
  const server = { ...empty, name: "Keep", kpis: [kpi("A", 100)] };
  assert.equal(mergeInterimDraft(server, server, { ...empty }).name, "Keep");
  assert.equal(mergeInterimDraft(server, server, { ...empty }).kpis.length, 1);
  assert.equal(mergeInterimDraft(server, server, null), server);
});

test("identical interim yields the same reference (no re-render storm)", () => {
  const server = { ...empty, name: "N", kpis: [kpi("A", 100)] };
  assert.equal(mergeInterimDraft(server, server, { ...server }), server);
  assert.equal(draftSignature(server), draftSignature({ ...server }));
});

test("sameEvents / weightedLeafProgress / stripEditSummary / failure messages", () => {
  const e = (id, message = "m", eventType = "x") => ({ id, message, eventType, actor: "master", round: 1, createdAt: "" });
  assert.ok(sameEvents([e("1"), e("2")], [e("1"), e("2")]));
  assert.ok(!sameEvents([e("1")], [e("1"), e("2")]));
  assert.ok(!sameEvents([e("1", "a")], [e("1", "b")]));
  const cat = kpi("Cat", null);
  const p = weightedLeafProgress([cat, kpi("A", 50, cat.id), kpi("B", null, cat.id)]);
  assert.deepEqual(p, { weighted: 1, total: 2 });
  assert.equal(stripEditSummary("hello\n\n[Edits I made directly in the live preview — x]"), "hello");
  assert.equal(stripEditSummary("plain"), "plain");
  for (const c of ["cancelled", "interrupted", "timeout", "bedrock_unavailable", "turn_failed", "weird"]) {
    assert.ok(turnFailureMessage(c).length > 10);
  }
  assert.notEqual(turnFailureMessage("cancelled"), turnFailureMessage("turn_failed"));
});
