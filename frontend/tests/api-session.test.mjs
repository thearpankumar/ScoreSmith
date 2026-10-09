// Browser-side session handling of the API client: cookies + CSRF header on every call, one shared refresh on a
// 401 and a retry, and a redirect to /login (keeping ?next=) when the session cannot be renewed.
import test from "node:test";
import assert from "node:assert/strict";

const assigned = [];
globalThis.window = { location: { pathname: "/charts/42", search: "?tab=evaluate", assign: (u) => assigned.push(u) } };
globalThis.document = { cookie: "theme=dark; qs_csrf=csrf-token-123; other=1", visibilityState: "visible", addEventListener() {}, removeEventListener() {} };

const calls = [];
const json = (status, body) => ({ ok: status < 400, status, statusText: "", text: async () => (body === undefined ? "" : JSON.stringify(body)) });
let handler = () => json(200, {});
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  return handler(String(url), init);
};

const api = await import("../lib/api-client.ts");

test("browser calls are same-origin, send cookies, and add the CSRF header on writes only", async () => {
  calls.length = 0;
  handler = () => json(200, []);
  await api.listScorecards().catch(() => {});
  assert.ok(calls[0].url.startsWith("/api/v1/scorecards"), calls[0].url); // no host: proxied by Next
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], undefined); // GET: not needed
  assert.equal(calls[0].init.headers["X-User-Id"], undefined);

  calls.length = 0;
  handler = () => json(204);
  await api.deleteScorecard("sc1");
  assert.equal(calls[0].init.method, "DELETE");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], "csrf-token-123");
});

test("a 401 refreshes the session once and retries the original request", async () => {
  calls.length = 0;
  let first = true;
  handler = (url) => {
    if (url.endsWith("/auth/refresh")) return json(200, { access_token: "t" });
    if (first) {
      first = false;
      return json(401, { detail: "Invalid or expired session." });
    }
    return json(204);
  };
  await api.deleteScorecard("sc2");
  assert.deepEqual(
    calls.map((c) => `${c.init.method ?? "GET"} ${c.url}`),
    ["DELETE /api/v1/scorecards/sc2", "POST /api/v1/auth/refresh", "DELETE /api/v1/scorecards/sc2"],
  );
  assert.equal(calls[1].init.headers["X-CSRF-Token"], "csrf-token-123");
  assert.equal(assigned.length, 0);
});

test("concurrent 401s share ONE refresh request", async () => {
  calls.length = 0;
  const seen = new Map();
  handler = (url) => {
    if (url.endsWith("/auth/refresh")) return new Promise((r) => setTimeout(() => r(json(200, {})), 40));
    const n = (seen.get(url) ?? 0) + 1;
    seen.set(url, n);
    return n === 1 ? json(401, { detail: "x" }) : json(204);
  };
  await Promise.all([api.deleteScorecard("a"), api.deleteScorecard("b"), api.deleteScorecard("c")]);
  assert.equal(calls.filter((c) => c.url.endsWith("/auth/refresh")).length, 1);
});

test("when the session cannot be renewed the browser is sent to the homepage (/?expired=1)", async () => {
  assigned.length = 0;
  calls.length = 0;
  handler = (url) => (url.endsWith("/auth/refresh") ? json(401, { detail: "Session expired." }) : json(401, { detail: "Invalid or expired session." }));
  await assert.rejects(api.deleteScorecard("sc3"), (e) => e.status === 401);
  assert.deepEqual(assigned, ["/?expired=1"]);
});

test("sign-in routes (authRoute) never refresh or redirect: a 401 just means wrong password", async () => {
  assigned.length = 0;
  calls.length = 0;
  handler = () => json(401, { detail: "Invalid email or password." });
  const body = { email: "a@b.co", password: "x", remember_me: false };
  await assert.rejects(
    api.authApiFetch("/api/v1/auth/login", { method: "POST", authRoute: true, body }),
    (e) => e.status === 401 && /Invalid email/.test(e.message),
  );
  assert.equal(calls.length, 1); // no refresh round-trip
  assert.equal(assigned.length, 0);
  assert.deepEqual(JSON.parse(calls[0].init.body), body);
});
