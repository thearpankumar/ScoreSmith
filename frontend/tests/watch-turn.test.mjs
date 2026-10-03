// Drives startChatTurn / watchChatTurn / cancelChatTurn against a mock backend that replays the
// real turn sequence: 202 -> interim drafts (header, KPIs+weights, guidelines) + trace events ->
// done (or failed / cancelled), including a network blip and an abort. No network, no deps.
import test from "node:test";
import assert from "node:assert/strict";

const calls = [];
let script = []; // GET /chat/sessions/{id} responses, consumed one per poll
let events = [];
globalThis.document = { visibilityState: "visible", addEventListener() {}, removeEventListener() {} };
globalThis.window = undefined;
const json = (status, body) => ({
  ok: status < 400, status, statusText: "", text: async () => (body === undefined ? "" : JSON.stringify(body)),
});
globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  const method = init.method ?? "GET";
  calls.push(`${method} ${u.replace(/^.*\/api\/v1/, "")}`);
  if (init.signal?.aborted) throw new DOMException("aborted", "AbortError");
  if (u.endsWith("/turn-events")) return json(200, events);
  if (method === "POST" && u.endsWith("/chat/sessions")) return json(202, turn({ turn_in_progress: true }));
  if (method === "POST" && u.endsWith("/messages")) return json(202, turn({ turn_in_progress: true }));
  if (method === "POST" && u.endsWith("/cancel")) return { ok: true, status: 204, statusText: "", text: async () => "" };
  if (u.includes("/users")) return json(200, [{ id: "u1", email: "designer@qualityscorecard.local", name: "A", role: "admin", created_at: "" }]);
  const next = script.shift();
  if (next instanceof Error) throw next;
  return json(next.status ?? 200, next.body);
};
const turn = (o) => ({ session_id: "sid", status: "gathering", draft: {}, turn_in_progress: false, title: "T", ...o });
const kp = (name, weight, g = {}) => ({ name, weight, level: 1, parent_name: null, guidelines: g });

const api = await import("../lib/api-client.ts");

test("start -> interim drafts -> done, polling stops, no extra polls after completion", async () => {
  calls.length = 0;
  script = [
    { body: turn({ turn_in_progress: true, draft: { name: "Sales QA", purpose: "p", target_score: 8 } }) },
    { body: turn({ turn_in_progress: true, draft: { name: "Sales QA", kpis: [kp("A", 60), kp("B", 40)] } }) },
    { body: turn({ turn_in_progress: true, draft: { name: "Sales QA", kpis: [kp("A", 60, { x: 1 }), kp("B", 40)] } }) },
    { body: turn({ status: "awaiting_clarification", assistant_message: "hi", question: { question: "Q?", options: ["a"], missing_fields: [] } }) },
  ];
  events = [{ id: "e1", session_id: "sid", turn_started_at: "", actor: "master", event_type: "guidelines", message: "Wrote guidelines for 1/2", round: 1, created_at: "" }];
  const started = await api.startChatTurn({ sessionId: "new", message: "hello", clientSessionId: "sid" });
  assert.equal(started.turnInProgress, true);
  assert.equal(started.sessionId, "sid");

  const ac = new AbortController();
  const seen = [];
  const final = await api.watchChatTurn("sid", { signal: ac.signal, onUpdate: (s, ev) => seen.push([s.draft.name, s.draft.kpis.length, ev?.length]) });
  assert.deepEqual(seen, [["Sales QA", 0, 1], ["Sales QA", 2, 1], ["Sales QA", 2, 1]]);
  assert.equal(final.turnInProgress, false);
  assert.equal(final.assistantText, "hi");
  assert.equal(final.clarifyingQuestion.options[0].label, "a");
  assert.equal(script.length, 0);
  assert.equal(calls.filter((c) => c.startsWith("GET /chat/sessions/sid") && !c.endsWith("events")).length, 4);
});

test("a network blip does not end the watch (it recovers and finishes)", async () => {
  script = [new TypeError("fetch failed"), { body: turn({ turn_in_progress: true }) }, { body: turn({ status: "gathering", assistant_message: "ok" }) }];
  const conn = [];
  const final = await api.watchChatTurn("sid", { signal: new AbortController().signal, onUpdate() {}, onConnection: (ok) => conn.push(ok) });
  assert.equal(final.assistantText, "ok");
  assert.deepEqual(conn, [false, true]);
}, { timeout: 15000 });

test("failed / cancelled turns surface the code; never while running", async () => {
  for (const code of ["cancelled", "timeout", "bedrock_unavailable", "interrupted", "turn_failed"]) {
    script = [{ body: turn({ turn_in_progress: true, turn_error: "x", turn_error_code: code }) }, { body: turn({ turn_error: "x", turn_error_code: code }) }];
    const final = await api.watchChatTurn("sid", { signal: new AbortController().signal, onUpdate: (s) => assert.equal(s.turnError, undefined) });
    assert.equal(final.turnError.code, code);
  }
}, { timeout: 15000 });

test("abort stops the watcher with AbortError and issues no further requests", async () => {
  script = [{ body: turn({ turn_in_progress: true }) }];
  const ac = new AbortController();
  let n = 0;
  const p = api.watchChatTurn("sid", { signal: ac.signal, onUpdate: () => { n++; ac.abort(); } });
  await assert.rejects(p, { name: "AbortError" });
  const before = calls.length;
  await new Promise((r) => setTimeout(r, 1500));
  assert.equal(calls.length, before);
  assert.equal(n, 1);
});

test("404 (deleted session) rejects; cancel hits POST /cancel; 409 is an ApiError", async () => {
  script = [{ status: 404, body: { detail: "nope" } }];
  await assert.rejects(api.watchChatTurn("sid", { signal: new AbortController().signal, onUpdate() {} }), { status: 404 });
  calls.length = 0;
  await api.cancelChatTurn("sid");
  assert.ok(calls.includes("POST /chat/sessions/sid/cancel"));
  const f = globalThis.fetch;
  globalThis.fetch = async (u, i) => (String(u).includes("/users") ? f(u, i) : json(409, { detail: "busy" }));
  await assert.rejects(api.startChatTurn({ sessionId: "sid", message: "x" }), { status: 409 });
  globalThis.fetch = f;
});

test("poll cadence: fast when visible, slower hidden, capped exponential backoff on errors", () => {
  const mid = () => 0.5; // jitter factor 1.0
  assert.equal(api.chatPollDelayMs(0, false), 1200);
  assert.equal(api.chatPollDelayMs(0, false, 90_000), 2000); // eases off on a long turn
  assert.equal(api.chatPollDelayMs(0, true), 5000);
  assert.deepEqual([1, 2, 3, 4, 5, 9].map((n) => api.chatPollDelayMs(n, false, 0, mid)), [2000, 4000, 8000, 15000, 15000, 15000]);
  assert.equal(api.chatPollDelayMs(2, false, 0, () => 0), 3200); // -20% jitter
  assert.equal(api.chatPollDelayMs(2, false, 0, () => 1), 4800); // +20% jitter
});
