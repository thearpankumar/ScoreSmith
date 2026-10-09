// Drives watchEvaluation against a mock backend: stops on completed / failed (incl. cancelled), keeps
// polling through queued -> ingesting -> scoring, survives a network blip, rejects on 404 and abort.
// Also checks the progress/evaluation snake->camel mapping. No network, no deps.
import test from "node:test";
import assert from "node:assert/strict";

const calls = [];
const progressHeaders = []; // fetch init of each progress request
let script = []; // GET /evaluations/{id}/progress responses, one per poll
globalThis.document = { visibilityState: "visible", addEventListener() {}, removeEventListener() {} };
globalThis.window = undefined;
const json = (status, body) => ({
  ok: status < 400, status, statusText: "", text: async () => (body === undefined ? "" : JSON.stringify(body)),
});
globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  calls.push(`${init.method ?? "GET"} ${u.replace(/^.*\/api\/v1/, "")}`);
  if (init.signal?.aborted) throw new DOMException("aborted", "AbortError");
  if (u.includes("/progress")) progressHeaders.push(init);
  if (/\/api\/v1\/me($|\?)/.test(u)) return json(200, { id: "u1", email: "designer@qualityscorecard.local", name: "A", role: "member", created_at: "" });
  if (/scorecard-versions|\/versions/.test(u)) return json(404, { detail: "nf" });
  const next = script.shift();
  if (next instanceof Error) throw next;
  return json(next.status ?? 200, next.body);
};
const prog = (o) => ({ evaluation_id: "ev1", status: "queued", stage: "queued", queue_position: null, error_code: null, error_message: null, progress: null, sources: [], events: [], ...o });

const api = await import("../lib/api-client.ts");

test("polls until completed and stops (no extra polls)", async () => {
  calls.length = 0;
  script = [
    { body: prog({ status: "queued", queue_position: 2 }) },
    { body: prog({ status: "ingesting", stage: "ingest", progress: { stage: "ingest", message: "Fetching", files: [{ source_id: "s1", name: "a.pdf", state: "running", detail: "d" }], counters: { files_total: 2, files_done: 1 } } }) },
    { body: prog({ status: "scoring", stage: "scoring:kpi" }) },
    { body: prog({ status: "completed", stage: "done" }) },
  ];
  const seen = [];
  const final = await api.watchEvaluation("ev1", { signal: new AbortController().signal, onUpdate: (p) => seen.push([p.status, p.queuePosition]) });
  assert.deepEqual(seen, [["queued", 2], ["ingesting", null], ["scoring", null], ["completed", null]]);
  assert.equal(final.status, "completed");
  assert.equal(script.length, 0);
  const polls = calls.filter((c) => !c.endsWith(" /me")); // the one-off "who am I" lookup is not a poll
  assert.equal(polls.length, 4);
  assert.ok(polls.every((c) => c === "GET /evaluations/ev1/progress"));
});

test("maps progress.json fields to camelCase", async () => {
  script = [{ body: prog({ status: "failed", error_code: "drive_inaccessible", error_message: "403", progress: { stage: "failed", message: "m", updated_at: "t", files: [{ source_id: "s1", name: "a.pdf", state: "failed", detail: "x" }], counters: { files_total: 2, files_done: 1, images_total: 5, images_done: 3, chunks_total: 4, chunks_done: 2 } }, sources: [{ id: "s1", kind: "drive", original_name: null, drive_url: "https://drive.google.com/x", size: null, status: "failed", warnings: ["w"] }], events: [{ id: "e1", created_at: "t", event_type: "x", message: "hello" }] }) }];
  const final = await api.watchEvaluation("ev1", { signal: new AbortController().signal, onUpdate() {} });
  assert.equal(final.errorCode, "drive_inaccessible");
  assert.equal(final.errorMessage, "403");
  assert.deepEqual(final.progress.counters, { filesTotal: 2, filesDone: 1, imagesTotal: 5, imagesDone: 3, chunksTotal: 4, chunksDone: 2 });
  assert.deepEqual(final.progress.files[0], { sourceId: "s1", name: "a.pdf", state: "failed", detail: "x" });
  assert.equal(final.sources[0].driveUrl, "https://drive.google.com/x");
  assert.deepEqual(final.sources[0].warnings, ["w"]);
  assert.equal(final.events[0].message, "hello");
});

test("failed (including cancelled) is terminal: the watch stops", async () => {
  calls.length = 0;
  script = [{ body: prog({ status: "scoring" }) }, { body: prog({ status: "failed", error_code: "cancelled" }) }, { body: prog({ status: "completed" }) }];
  const final = await api.watchEvaluation("ev1", { signal: new AbortController().signal, onUpdate() {} });
  assert.equal(final.status, "failed");
  assert.equal(final.errorCode, "cancelled");
  assert.equal(script.length, 1); // the trailing completed was never polled
  assert.equal(calls.length, 2);
});

test("a network blip does not end the watch", async () => {
  script = [new TypeError("fetch failed"), { body: prog({ status: "processing" }) }, { body: prog({ status: "completed" }) }];
  const conn = [];
  const final = await api.watchEvaluation("ev1", { signal: new AbortController().signal, onUpdate() {}, onConnection: (ok) => conn.push(ok) });
  assert.equal(final.status, "completed");
  assert.deepEqual(conn, [false, true]);
});

test("404 rejects with ApiError", async () => {
  script = [{ status: 404, body: { detail: "Evaluation not found" } }];
  await assert.rejects(
    api.watchEvaluation("ev1", { signal: new AbortController().signal, onUpdate() {} }),
    (e) => e.status === 404 && /not found/i.test(e.message),
  );
});

test("aborting rejects with AbortError and stops polling", async () => {
  calls.length = 0;
  script = Array.from({ length: 20 }, () => ({ body: prog({ status: "processing" }) }));
  const ac = new AbortController();
  let n = 0;
  await assert.rejects(
    api.watchEvaluation("ev1", { signal: ac.signal, onUpdate: () => { if (++n === 2) ac.abort(); } }),
    (e) => e.name === "AbortError",
  );
  assert.equal(n, 2);
  assert.ok(calls.length <= 3);
});

test("evaluation list rows map the new AI fields; 422 detail arrays are readable", async () => {
  // getAiBatch maps evaluations through the shared enrichment path; versions/users are mocked empty.
  script = [
    { body: { id: "b1", scorecard_id: "sc", status: "running", total: 2, counts: { queued: 1, running: 1, completed: 0, failed: 0 }, evaluations: [{ id: "e1", scorecard_version_id: "v1", name: "n", evaluated_by: "u", input_reference: null, status: "queued", final_weighted_score: null, rag_band: null, submitted_at: null, domain: null, created_at: "2026-01-01T00:00:00Z", stage: "queued", subject_name: "Ann", subject_email: "a@x.io", batch_id: "b1", error_code: null, queued_at: "q", started_at: null, finished_at: null }] } },
  ];
  const batch = await api.getAiBatch("b1");
  assert.equal(batch.total, 2);
  assert.equal(batch.counts.running, 1);
  const e = batch.evaluations[0];
  assert.equal(e.status, "queued");
  assert.equal(e.subjectName, "Ann");
  assert.equal(e.subjectEmail, "a@x.io");
  assert.equal(e.batchId, "b1");
  assert.equal(e.queuedAt, "q");

  script = [{ status: 422, body: { detail: [{ loc: ["body", "items", 0, "sources", 0, "drive_url"], msg: "host not allowed" }] } }];
  await assert.rejects(
    api.createAiJobs({ scorecardId: "sc", items: [{ sources: [{ kind: "drive", driveUrl: "https://x.com" }] }] }),
    (err) => err.status === 422 && /host not allowed/.test(err.message),
  );
});

test("progress polls ride on the session cookie (credentials: include) and never send a user-id header", async () => {
  progressHeaders.length = 0;
  script = [{ body: prog({ status: "completed", stage: "done" }) }];
  await api.watchEvaluation("ev1", { signal: new AbortController().signal, onUpdate() {} });
  assert.equal(progressHeaders.length, 1);
  assert.equal(progressHeaders[0].credentials, "include");
  assert.equal(progressHeaders[0].headers["X-User-Id"], undefined);
  assert.equal(progressHeaders[0].headers["x-user-id"], undefined);
});
