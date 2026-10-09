// Request building and error mapping of the sharing / notification / evaluations-page client (lib/collab-client.ts).
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

register("./fixtures/alias-loader.mjs", import.meta.url);
globalThis.window = { location: { pathname: "/evaluations", search: "", assign() {} } };
globalThis.document = { cookie: "qs_csrf=tok", visibilityState: "visible", addEventListener() {}, removeEventListener() {} };

const calls = [];
const resp = (status, body, headers = {}) => ({
  ok: status < 400,
  status,
  statusText: "",
  headers: { get: (k) => headers[k] ?? headers[k.toLowerCase()] ?? null },
  text: async () => (body === undefined ? "" : JSON.stringify(body)),
  json: async () => body,
});
let handler = () => resp(200, {});
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  return handler(String(url), init);
};

const c = await import("../lib/collab-client.ts");
const { ApiError } = await import("../lib/api-client.ts");
const { timeAgo } = await import("../lib/time.ts");

test("evaluation filters become query strings and JSON bodies, and only active filters are sent", () => {
  assert.equal(c.evalQueryString(c.DEFAULT_EVAL_QUERY), "status=all&sort=newest");
  const q = {
    ...c.DEFAULT_EVAL_QUERY,
    status: "completed",
    scorecardId: "sc1",
    bands: ["band_8", "band_7"],
    minScore: "5",
    meetsTarget: "no",
    q: "  alpha  ",
    runner: "others",
    shared: "shared",
    dateFrom: "2026-10-01",
    sort: "score_desc",
  };
  const qs = new URLSearchParams(c.evalQueryString(q, { limit: "40", cursor: "abc" }));
  assert.equal(qs.get("status"), "completed");
  assert.equal(qs.get("scorecard_id"), "sc1");
  assert.deepEqual(qs.getAll("band"), ["band_8", "band_7"]);
  assert.equal(qs.get("min_score"), "5");
  assert.equal(qs.get("meets_target"), "false");
  assert.equal(qs.get("q"), "alpha");
  assert.equal(qs.get("runner"), "others");
  assert.equal(qs.get("shared"), "true");
  assert.equal(qs.get("date_from"), "2026-10-01");
  assert.equal(qs.get("sort"), "score_desc");
  assert.equal(qs.get("limit"), "40");
  assert.equal(qs.get("cursor"), "abc");
  const body = c.evalFilterBody(q);
  assert.equal(body.meets_target, false);
  assert.equal("max_score" in body, false);
  assert.equal("sort" in body, false);
});

test("a page request maps rows to camelCase and keeps the cursor and totals", async () => {
  calls.length = 0;
  handler = () =>
    resp(200, {
      items: [
        {
          id: "e1", name: "N", status: "completed", stage: null, final_weighted_score: 7.5, rag_band: "band_7", created_at: "t", submitted_at: null,
          queued_at: null, started_at: null, finished_at: null, subject_name: null, subject_email: null, batch_id: null, error_code: null,
          error_message: null, attempt: 1, domain: "D", scorecard_id: "sc", scorecard_version_id: "v", scorecard_name: "S", target_score: 7,
          runner_id: "u", runner_name: "Ann", is_mine: false, shared: true,
        },
      ],
      next_cursor: "CUR", total: 123, selectable: 100, exportable: 90, total_capped: false, count_cap: 10000,
    });
  const page = await c.fetchEvaluationsPage({ ...c.DEFAULT_EVAL_QUERY, q: "x" }, { limit: 10 });
  assert.ok(calls[0].url.startsWith("/api/v1/evaluations/page?"));
  assert.equal(page.nextCursor, "CUR");
  assert.deepEqual([page.total, page.selectable, page.exportable], [123, 100, 90]);
  assert.deepEqual([page.items[0].runnerName, page.items[0].isMine, page.items[0].shared, page.items[0].finalWeightedScore], ["Ann", false, true, 7.5]);
});

test("bulk delete by filter sends the filter and the un-ticked ids, never a list of loaded rows", async () => {
  calls.length = 0;
  handler = () => resp(200, { deleted: 3, skipped: 0 });
  await c.bulkDeleteEvaluations({ filter: { ...c.DEFAULT_EVAL_QUERY, status: "failed" }, excludeIds: ["k1"] });
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { filter: { status: "failed" }, exclude_ids: ["k1"] });
  await c.bulkDeleteEvaluations({ ids: ["a", "b"] });
  assert.deepEqual(JSON.parse(calls[1].init.body), { ids: ["a", "b"] });
});

test("unread-count is a conditional request: 304 keeps the state, 200 carries the new ETag", async () => {
  calls.length = 0;
  handler = () => resp(200, { unread: 4 }, { ETag: '"v1"' });
  const first = await c.fetchUnreadCount(null);
  assert.deepEqual(first, { changed: true, etag: '"v1"', unread: 4 });
  assert.equal(calls[0].init.headers["If-None-Match"], undefined);
  handler = () => resp(304, undefined);
  const again = await c.fetchUnreadCount('"v1"');
  assert.deepEqual(again, { changed: false, etag: '"v1"' });
  assert.equal(calls[1].init.headers["If-None-Match"], '"v1"');
});

test("structured API errors expose a machine-readable code and the blocking job", async () => {
  handler = () =>
    resp(409, {
      detail: { code: "user_job_active", message: "You already have an evaluation running.", evaluation_id: "e9", batch_id: null, scorecard_id: "sc9" },
    });
  await assert.rejects(c.getMySlots(), (err) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 409);
    assert.equal(err.code, "user_job_active");
    assert.equal(err.message, "You already have an evaluation running.");
    assert.equal(err.data.evaluation_id, "e9");
    assert.equal(c.activeJobHref(err), "/charts/sc9/evaluations/e9");
    return true;
  });
  handler = () => resp(404, { detail: { code: "user_not_found", message: "No active user matches that username or email." } });
  await assert.rejects(c.inviteCollaborator("sc", "ghost"), (err) => err.code === "user_not_found" && /No active user/.test(err.message));
  handler = () => resp(409, { detail: "plain string errors still work" });
  await assert.rejects(c.getMySlots(), (err) => err.code === undefined && err.message === "plain string errors still work");
});

test("a batch conflict links to the batch", () => {
  const err = new ApiError("x", 409, { code: "user_job_active", data: { batch_id: "b1", evaluation_id: "e1", scorecard_id: "s1" } });
  assert.equal(c.activeJobHref(err), "/evaluations?batch=b1");
  assert.equal(c.activeJobHref(new ApiError("x", 409, { code: "other" })), null);
});

test("timeAgo is short and human", () => {
  const now = Date.parse("2026-10-10T12:00:00Z");
  assert.equal(timeAgo("2026-10-10T11:59:50Z", now), "just now");
  assert.equal(timeAgo("2026-10-10T11:55:00Z", now), "5 min ago");
  assert.equal(timeAgo("2026-10-10T09:00:00Z", now), "3 h ago");
  assert.equal(timeAgo("2026-10-09T10:00:00Z", now), "yesterday");
  assert.equal(timeAgo("2026-10-07T12:00:00Z", now), "3 days ago");
  assert.equal(timeAgo("nonsense", now), "");
});
