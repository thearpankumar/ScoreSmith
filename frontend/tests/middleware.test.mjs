// Route protection middleware: public pages pass, a valid access cookie passes, an expired one is renewed through
// the backend (cookies forwarded + merged), a refused renewal redirects to /login?next=, an unreachable backend
// never signs anyone out, and the matcher skips the API proxy and static files.
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

register("./fixtures/alias-loader.mjs", import.meta.url);
const { middleware, config } = await import("../middleware.ts");
const { NextRequest } = await import("next/server");

const jwt = (exp) => `x.${Buffer.from(JSON.stringify({ exp })).toString("base64url")}.y`;
const now = () => Math.floor(Date.now() / 1000);
const fresh = () => jwt(now() + 600);
const stale = () => jwt(now() - 60);

const calls = [];
let backend = () => new Response("{}", { status: 200 });
globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), init });
  return backend(String(url), init);
};

function req(path, cookies = {}) {
  const cookie = Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");
  return new NextRequest(`http://localhost:3000${path}`, { headers: cookie ? { cookie } : {} });
}

const header = (res, name) => res.headers.get(name);
const isNext = (res) => header(res, "x-middleware-next") === "1";
const redirectTarget = (res) => new URL(header(res, "location"));

function refreshOk() {
  return () => {
    const h = new Headers();
    h.append("set-cookie", "qs_access=NEWACCESS; Path=/; HttpOnly");
    h.append("set-cookie", "qs_refresh=NEWREFRESH; Path=/; HttpOnly");
    return new Response(JSON.stringify({}), { status: 200, headers: h });
  };
}

test("public pages never need a session and never call the backend", async () => {
  calls.length = 0;
  for (const p of ["/login", "/signup", "/setup", "/forgot-password", "/reset-password?token=abc", "/verify-email"]) {
    assert.ok(isNext(await middleware(req(p))), p);
  }
  assert.equal(calls.length, 0);
});

test("a valid, unexpired access cookie lets the page render without a refresh round-trip", async () => {
  calls.length = 0;
  const res = await middleware(req("/chat", { qs_access: fresh() }));
  assert.ok(isNext(res));
  assert.equal(calls.length, 0);
});

test("no cookies at all: redirected to /login?next=<path+query>, not flagged expired, no backend call", async () => {
  calls.length = 0;
  const res = await middleware(req("/charts/42?tab=evaluate"));
  assert.equal(res.status, 307);
  const to = redirectTarget(res);
  assert.equal(to.pathname, "/login");
  assert.equal(to.searchParams.get("next"), "/charts/42?tab=evaluate");
  assert.equal(to.searchParams.get("expired"), null);
  assert.equal(calls.length, 0);
});

test("the homepage is public: no cookies, no redirect, no backend call", async () => {
  calls.length = 0;
  assert.ok(isNext(await middleware(req("/"))));
  assert.equal(calls.length, 0);
});

test("an expired access token is renewed with the refresh cookie, CSRF header and our origin", async () => {
  calls.length = 0;
  backend = refreshOk();
  const res = await middleware(req("/evaluations", { qs_access: stale(), qs_refresh: "R1", qs_csrf: "C1", theme: "dark" }));
  assert.ok(isNext(res));
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith("/api/v1/auth/refresh"));
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["x-csrf-token"], "C1");
  assert.equal(calls[0].init.headers.origin, "http://localhost:3000");
  assert.match(calls[0].init.headers.cookie, /qs_refresh=R1/);
  // the browser receives the new Set-Cookie headers
  const setCookies = res.headers.getSetCookie();
  assert.ok(setCookies.some((c) => c.startsWith("qs_access=NEWACCESS")));
  assert.ok(setCookies.some((c) => c.startsWith("qs_refresh=NEWREFRESH")));
  // and the Server Components of THIS request already see them (request-header override), other cookies kept
  const override = header(res, "x-middleware-request-cookie");
  assert.match(override, /qs_access=NEWACCESS/);
  assert.match(override, /qs_refresh=NEWREFRESH/);
  assert.match(override, /theme=dark/);
  assert.doesNotMatch(override, /qs_refresh=R1/);
});

test("a missing access cookie with a refresh cookie is also renewed", async () => {
  calls.length = 0;
  backend = refreshOk();
  assert.ok(isNext(await middleware(req("/settings", { qs_refresh: "R2", qs_csrf: "C2" }))));
  assert.equal(calls.length, 1);
});

test("a malformed access cookie counts as expired", async () => {
  calls.length = 0;
  backend = refreshOk();
  assert.ok(isNext(await middleware(req("/chat", { qs_access: "not-a-jwt", qs_refresh: "R" }))));
  assert.equal(calls.length, 1);
});

test("a refused renewal redirects to /login with next + expired and deletes the dead cookies", async () => {
  backend = () => new Response("{}", { status: 401 });
  const res = await middleware(req("/charts/7", { qs_access: stale(), qs_refresh: "dead", qs_csrf: "c" }));
  assert.equal(res.status, 307);
  const to = redirectTarget(res);
  assert.equal(to.pathname, "/login");
  assert.equal(to.searchParams.get("next"), "/charts/7");
  assert.equal(to.searchParams.get("expired"), "1");
  const cleared = res.headers.getSetCookie();
  for (const name of ["qs_access", "qs_refresh", "qs_csrf"]) {
    assert.ok(cleared.some((c) => c.startsWith(`${name}=`) && /Max-Age=0|Expires=Thu, 01 Jan 1970/i.test(c)), name);
  }
});

test("an expired access token without a refresh cookie goes to login flagged expired (it had a session)", async () => {
  const to = redirectTarget(await middleware(req("/chat", { qs_access: stale() })));
  assert.equal(to.searchParams.get("expired"), "1");
});

test("an unreachable backend keeps the visitor signed in instead of redirecting", async () => {
  backend = () => {
    throw new TypeError("fetch failed");
  };
  const res = await middleware(req("/chat", { qs_access: stale(), qs_refresh: "R" }));
  assert.ok(isNext(res));
  assert.equal(res.headers.getSetCookie().length, 0);
});

test("a 5xx from the backend is treated as a rejected renewal, never as success", async () => {
  backend = () => new Response("oops", { status: 500 });
  const res = await middleware(req("/chat", { qs_access: stale(), qs_refresh: "R" }));
  assert.equal(res.status, 307);
});

test("a forged but unexpired cookie only passes this UX gate; the signature is the backend's job", async () => {
  assert.ok(isNext(await middleware(req("/chat", { qs_access: fresh() }))));
});

test("matcher: pages are covered, the API proxy / Next internals / static files are not", () => {
  const [pattern] = config.matcher;
  const re = new RegExp(`^${pattern}$`);
  for (const p of ["/", "/chat", "/charts/1", "/login", "/charts/a.b/evaluations"]) assert.ok(re.test(p), `should match ${p}`);
  for (const p of ["/api/v1/me", "/api/", "/_next/static/chunk.js", "/_next/image", "/favicon.ico", "/logo.png", "/robots.txt", "/a/b.css"]) {
    assert.ok(!re.test(p), `should skip ${p}`);
  }
});
