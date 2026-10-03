// Replays a REAL recorded backend turn (tests/fixtures/real-turn.json: GET /chat/sessions/{id} +
// /turn-events + /messages captured from the running API for a 5-KPI list message) through the
// frontend's API mapping and state helpers, and checks the edit-summary wording against the backend's
// own pinned-KPI verification rule (a JS port of `_CHANGE_INTENT`/`_verified_user_changes`).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { dedupeRetriedMessages, describeDraftEdits, stripEditSummary, stripTrailingQuestion, weightedLeafProgress } from "../lib/chat-turn-state.ts";

const fx = JSON.parse(readFileSync(new URL("./fixtures/real-turn.json", import.meta.url), "utf8"));
globalThis.document = { visibilityState: "visible", addEventListener() {}, removeEventListener() {} };
globalThis.window = undefined;
const json = (status, body) => ({ ok: status < 400, status, statusText: "", text: async () => JSON.stringify(body) });
let i = 0;
globalThis.fetch = async (url) => {
  const u = String(url);
  const step = fx.sequence[Math.min(i, fx.sequence.length - 1)];
  if (u.endsWith("/turn-events")) return json(200, step.events);
  const r = json(200, step.turn);
  i++;
  return r;
};
const api = await import("../lib/api-client.ts");

test("real recorded turn: interim states map correctly, final carries the question + multi-paragraph reply", async () => {
  i = 0;
  const seen = [];
  const final = await api.watchChatTurn("s", { signal: new AbortController().signal, onUpdate: (snap, ev) => seen.push({ snap, ev }) });
  // t=1.2: empty draft but the title is already there
  assert.equal(seen[0].snap.draft.kpis.length, 0);
  assert.equal(seen[0].snap.title, "Customer Support Email Evaluation Scorecard");
  // t=2.5: KPIs exist, no weights, all guidelines pending
  const early = seen[1].snap.draft;
  assert.equal(early.kpis.length, 5);
  assert.deepEqual(weightedLeafProgress(early.kpis), { weighted: 0, total: 5 });
  assert.ok(early.kpis.every((k) => k.weight === null && k.guidelinesPending));
  // t=23.5: weights + guidelines in
  const mid = seen[2].snap.draft;
  assert.deepEqual(weightedLeafProgress(mid.kpis), { weighted: 5, total: 5 });
  assert.ok(mid.kpis.every((k) => !k.guidelinesPending && k.status === "confirmed"));
  assert.equal(mid.kpis.reduce((a, k) => a + k.weight, 0), 100);
  // done
  assert.equal(final.turnInProgress, false);
  assert.equal(final.status, "awaiting_clarification");
  assert.equal(final.clarifyingQuestion.options.length, 4);
  assert.match(final.assistantText, /\n\n/); // answer to the side question, then the summary, then the question
  // the bubble must not repeat the question the card shows
  const bubble = stripTrailingQuestion(final.assistantText, final.clarifyingQuestion.question);
  assert.ok(!bubble.includes("Would you like to save this scorecard as-is"));
  assert.match(bubble, /Why is this scorecard needed\?/);
});

// --- port of the backend verification rule (scorecard_builder.py) ---
const CHANGE_INTENT = /\b(remove|delete|drop|get rid|rename|replace|swap|move|regroup|merge|combine|split|without|instead|exclude|change|re-?weigh\w*|weigh\w*|weights?|priorit\w*|important|importance|increase|decrease|raise|lower|reduce|rewrite|reword|update|adjust|edit|modify|tweak|guidelines?|thresholds?|criteria|bump|make|set|scale|rebalance|add)\b/i;
const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const verified = (names, window) => (CHANGE_INTENT.test(window) ? names.filter((n) => ` ${norm(window)} `.includes(` ${norm(n)} `)) : []);

const kpi = (name, weight, parentId = null, extra = {}) => ({ id: `draft-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`, name, weight, level: 1, parentId, status: "confirmed", includedInScoring: true, ...extra });
const base = { sessionId: "s", name: "N", domain: null, purposeStatement: null, scope: null, targetScore: 8, scoringFormula: null,
  kpis: [kpi("Response Time", 25), kpi("Empathy", 18), kpi("Resolution Quality", 30), kpi("Clarity", 15), kpi("Professionalism", 12)] };
const msg = (local) => `Please apply the edits I made in the live preview.\n\n${describeDraftEdits(base, local)}`;

test("the OLD wording (bare list) would NOT be verified by the backend; the new per-KPI lines are", () => {
  const oldStyle = 'Please apply the edits I made in the live preview.\n\n[Edits I made directly in the live preview — please apply them to the draft:\n- KPI list should now be exactly: "Empathy" 20% ...]';
  assert.deepEqual(verified(["Empathy"], oldStyle), []);
  const local = { ...base, kpis: base.kpis.map((k) => (k.name === "Empathy" ? { ...k, weight: 20 } : k)) };
  const m = msg(local);
  assert.match(m, /Change the weight of "Empathy" from 18% to 20%/);
  assert.deepEqual(verified(["Empathy"], m), ["Empathy"]);
  assert.deepEqual(verified(["Clarity", "Response Time"], m), []); // untouched pinned KPIs stay protected
});

test("rename / remove / exclude / move / add lines carry the ORIGINAL name + an intent word", () => {
  const cat = kpi("Quality", null);
  const local = { ...base, kpis: [
    { ...kpi("Response Time", 25), name: "First Reply Time" },
    kpi("Empathy", 18, null, { includedInScoring: false }),
    { ...kpi("Resolution Quality", 30), parentId: cat.id },
    cat,
    kpi("Professionalism", 12),
    { ...kpi("Brand Voice", 10), id: "local-1" },
  ] };
  const m = msg(local);
  assert.deepEqual(verified(["Response Time"], m), ["Response Time"]);
  assert.match(m, /Rename the KPI "Response Time" to "First Reply Time"/);
  assert.match(m, /Exclude "Empathy" from the weighted score/);
  assert.match(m, /Move the KPI "Resolution Quality" from the top level to "Quality"/);
  assert.match(m, /Remove KPIs: "Clarity"/);
  assert.match(m, /Add a new KPI "Brand Voice"/);
  assert.deepEqual(verified(["Clarity"], m), ["Clarity"]);
  assert.equal(stripEditSummary(m), "Please apply the edits I made in the live preview.");
});

test("no edits -> null; formula edit is carried", () => {
  assert.equal(describeDraftEdits(base, { ...base }), null);
  const withFormula = describeDraftEdits(base, { ...base, scoringFormula: "min(a, b)" });
  assert.match(withFormula, /Scoring formula: set it to exactly "min\(a, b\)"/);
});

test("stripTrailingQuestion keeps text that does not end with the question and never empties a bubble", () => {
  assert.equal(stripTrailingQuestion("Hello", "Q?"), "Hello");
  assert.equal(stripTrailingQuestion("Q?", "Q?"), "Q?");
  assert.equal(stripTrailingQuestion("A.\n\nQ?", "Q?"), "A.");
});

test("a retried first message is shown once; real repeated questions after a reply are kept", () => {
  const u = (c) => ({ role: "user", content: c });
  const a = (c) => ({ role: "assistant", content: c });
  assert.deepEqual(dedupeRetriedMessages([u("x"), u("x"), a("ok")]), [u("x"), a("ok")]);
  assert.equal(dedupeRetriedMessages([u("x"), a("ok"), u("x")]).length, 3);
});

test("a differently-worded closing question paragraph is dropped when a card shows the question", () => {
  const body = ["Summary.", "- A 50%", "Would you like to save as-is, or adjust anything (weights, target)?"].join("\n\n");
  assert.equal(stripTrailingQuestion(body, "Would you like to save as-is, or adjust something first?"), ["Summary.", "- A 50%"].join("\n\n"));
  assert.equal(stripTrailingQuestion("Only one paragraph?", "Other?"), "Only one paragraph?");
});
