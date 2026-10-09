// More of the browser API client's session handling: error formatting, refresh edge cases, single-flight reset,
// no refresh on 403/429, and no redirect loop from the login page.
import test from "node:test";
import assert from "node:assert/strict";

const assigned = [];
const loc = { pathname: "/evaluations", search: "?page=2", assign: (u) => assigned.push(u) };
globalThis.window = { location: loc };
let cookie = "qs_csrf=csrf-1";
globalThis.document = {
  get cookie() {
    return cookie;
  },
  visibilityState: "visible",
  addEventListener() {},
  removeEventListener() {},
};

const calls = [];
const json = (status, body, statusText = "") => ({ ok: status < 400, status, statusText, text: async () => (body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body)) });
let handler = () => json(200, {});
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  return handler(String(url), init);
};

const api = await import("../lib/api-client.ts");
const refreshCalls = () => calls.filter((c) => c.url.endsWith("/auth/refresh")).length;
const reset = () => {
  calls.length = 0;
  assigned.length = 0;
  loc.pathname = "/evaluations";
  loc.search = "?page=2";
};

test("a 422 list detail becomes 'field: message' pairs joined with semicolons", async () => {
  reset();
  handler = () =>
    json(422, {
      detail: [
        { loc: ["body", "name"], msg: "Field required" },
        { loc: ["query", "limit"], msg: "Input should be less than or equal to 200" },
        { msg: "no location" },
      ],
    });
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 422 && e.message === "name: Field required; query.limit: Input should be less than or equal to 200; no location");
});

test("non-JSON and empty error bodies fall back to the text or the status line", async () => {
  reset();
  handler = () => json(500, "Internal Server Error");
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 500 && e.message === "Internal Server Error");
  handler = () => json(503, undefined, "Service Unavailable");
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 503 && e.message === "Service Unavailable");
  handler = () => json(500, undefined, "");
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 500 && /Request failed \(500\)/.test(e.message));
});

test("HTTP 502 becomes a BedrockUnavailableError and a dead network a status-0 ApiError", async () => {
  reset();
  handler = () => json(502, { detail: "The AI model is unavailable." });
  await assert.rejects(api.deleteScorecard("x"), (e) => e instanceof api.BedrockUnavailableError && e.status === 502);
  handler = () => {
    throw new TypeError("fetch failed");
  };
  await assert.rejects(api.deleteScorecard("x"), (e) => e instanceof api.ApiError && e.status === 0 && /Could not reach the backend/.test(e.message));
});

test("Next.js redirect / dynamic-usage sentinels pass through untouched", async () => {
  reset();
  for (const digest of ["NEXT_REDIRECT;replace;/login;307;", "DYNAMIC_SERVER_USAGE: x"]) {
    const sentinel = Object.assign(new Error("next"), { digest });
    handler = () => {
      throw sentinel;
    };
    await assert.rejects(api.deleteScorecard("x"), (e) => e === sentinel);
  }
});

test("403 (CSRF/forbidden) and 429 (rate limit) never trigger a refresh or a redirect", async () => {
  reset();
  for (const status of [403, 404, 409, 429]) {
    handler = () => json(status, { detail: `status ${status}` });
    await assert.rejects(api.deleteScorecard("x"), (e) => e.status === status);
  }
  assert.equal(refreshCalls(), 0);
  assert.equal(assigned.length, 0);
});

test("a refresh that throws (network) ends in the login redirect, once", async () => {
  reset();
  handler = (url) => {
    if (url.endsWith("/auth/refresh")) throw new TypeError("offline");
    return json(401, { detail: "Invalid or expired session." });
  };
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 401);
  assert.deepEqual(assigned, ["/?expired=1"]);
});

test("a refresh that succeeds but the retry is still 401 redirects instead of looping", async () => {
  reset();
  handler = (url) => (url.endsWith("/auth/refresh") ? json(200, {}) : json(401, { detail: "nope" }));
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 401);
  assert.equal(refreshCalls(), 1);
  assert.equal(calls.filter((c) => !c.url.endsWith("/auth/refresh")).length, 2); // original + exactly one retry
  assert.equal(assigned.length, 1);
});

test("no redirect is issued while already on a /login page (no loop)", async () => {
  reset();
  loc.pathname = "/login";
  handler = () => json(401, { detail: "nope" });
  await assert.rejects(api.deleteScorecard("x"), (e) => e.status === 401);
  assert.equal(assigned.length, 0);
});

test("the single-flight refresh is released afterwards, so a later 401 refreshes again", async () => {
  reset();
  let n = 0;
  handler = (url) => {
    if (url.endsWith("/auth/refresh")) return json(200, {});
    n += 1;
    return n % 2 === 1 ? json(401, {}) : json(204);
  };
  await api.deleteScorecard("a");
  await api.deleteScorecard("b");
  assert.equal(refreshCalls(), 2);
});

test("the CSRF header is empty (not undefined) when the cookie is missing, and is read fresh each call", async () => {
  reset();
  cookie = "theme=dark";
  handler = () => json(204);
  await api.deleteScorecard("a");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], "");
  cookie = "theme=dark; qs_csrf=rotated";
  await api.deleteScorecard("b");
  assert.equal(calls[1].init.headers["X-CSRF-Token"], "rotated");
  cookie = "qs_csrf=csrf-1";
});

test("the refresh request itself carries the CSRF header, cookies and no-store", async () => {
  reset();
  handler = (url) => (url.endsWith("/auth/refresh") ? json(200, {}) : calls.filter((c) => !c.url.endsWith("/auth/refresh")).length === 1 ? json(401, {}) : json(204));
  await api.deleteScorecard("x");
  const refresh = calls.find((c) => c.url.endsWith("/auth/refresh"));
  assert.equal(refresh.init.method, "POST");
  assert.equal(refresh.init.credentials, "include");
  assert.equal(refresh.init.headers["X-CSRF-Token"], "csrf-1");
  assert.equal(refresh.init.cache, "no-store");
});

test("a 204 resolves to undefined and a JSON body round-trips", async () => {
  reset();
  handler = () => json(204);
  assert.equal(await api.deleteScorecard("x"), undefined);
});
