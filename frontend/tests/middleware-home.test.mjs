// Homepage / sign-in pages for visitors WITH a session: straight to /chat (no homepage flash), silent renewal on the
// way, safe ?next= on /login, no loops, logged-out visitors keep the homepage, protected paths keep ?next=.
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

register("./fixtures/alias-loader.mjs", import.meta.url);
const { middleware } = await import("../middleware.ts");
const { NextRequest } = await import("next/server");

const jwt = (exp) => `x.${Buffer.from(JSON.stringify({ exp })).toString("base64url")}.y`;
const now = () => Math.floor(Date.now() / 1000);
const fresh = () => jwt(now() + 600);
const stale = () => jwt(now() - 60);

const calls = [];
let backend = () => new Response("{}", { status: 200 });
globalThis.fetch = async (url, init) => {
  calls.push(String(url));
  return backend(String(url), init);
};
const req = (path, cookies = {}) => {
  const cookie = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  return new NextRequest(`http://localhost:3000${path}`, { headers: cookie ? { cookie } : {} });
};
const isNext = (res) => res.headers.get("x-middleware-next") === "1";
const target = (res) => {
  assert.equal(res.status, 307);
  const u = new URL(res.headers.get("location"));
  return u.pathname + u.search;
};
const renewOk = () => {
  const h = new Headers();
  h.append("set-cookie", "qs_access=NEW; Path=/; HttpOnly");
  h.append("set-cookie", "qs_refresh=NEWR; Path=/; HttpOnly");
  return new Response("{}", { status: 200, headers: h });
};
const cleared = (res) => res.headers.getSetCookie().filter((c) => /Max-Age=0|Expires=Thu, 01 Jan 1970/i.test(c)).map((c) => c.split("=")[0]);

test("valid access cookie on / goes straight to /chat without calling the backend", async () => {
  calls.length = 0;
  assert.equal(target(await middleware(req("/", { qs_access: fresh() }))), "/chat");
  assert.equal(calls.length, 0);
});

test("refresh-only cookie on /: renewed, redirected to /chat, new cookies set on the redirect", async () => {
  backend = renewOk;
  const res = await middleware(req("/", { qs_refresh: "R", qs_csrf: "C" }));
  assert.equal(target(res), "/chat");
  const sc = res.headers.getSetCookie();
  assert.ok(sc.some((c) => c.startsWith("qs_access=NEW")));
  assert.ok(sc.some((c) => c.startsWith("qs_refresh=NEWR")));
});

test("expired access + refresh cookie on / behaves the same", async () => {
  backend = renewOk;
  assert.equal(target(await middleware(req("/", { qs_access: stale(), qs_refresh: "R" }))), "/chat");
});

test("failed renewal: the homepage renders and the dead cookies are cleared", async () => {
  backend = () => new Response("{}", { status: 401 });
  const res = await middleware(req("/", { qs_access: stale(), qs_refresh: "dead", qs_csrf: "c" }));
  assert.ok(isNext(res));
  assert.deepEqual(cleared(res).sort(), ["qs_access", "qs_csrf", "qs_refresh"]);
});

test("unreachable backend: homepage renders, cookies are kept", async () => {
  backend = () => {
    throw new TypeError("fetch failed");
  };
  const res = await middleware(req("/", { qs_refresh: "R" }));
  assert.ok(isNext(res));
  assert.equal(cleared(res).length, 0);
});

test("logged out: / and /login render, nothing is called or cleared", async () => {
  calls.length = 0;
  for (const p of ["/", "/login", "/signup"]) {
    const res = await middleware(req(p));
    assert.ok(isNext(res), p);
    assert.equal(res.headers.getSetCookie().length, 0);
  }
  assert.equal(calls.length, 0);
});

test("/login, /signup, /forgot-password, /reset-password, /setup with a session -> /chat", async () => {
  for (const p of ["/login", "/signup", "/forgot-password", "/reset-password?token=t", "/setup"]) {
    assert.equal(target(await middleware(req(p, { qs_access: fresh() }))), "/chat", p);
  }
});

test("/login with a session honours a safe ?next= and ignores unsafe ones (no open redirect)", async () => {
  const c = { qs_access: fresh() };
  assert.equal(target(await middleware(req("/login?next=%2Fcharts%2F4%3Ftab%3Dx", c))), "/charts/4?tab=x");
  for (const bad of ["//evil.example", "https://evil.example", "/%5Cevil.example", "javascript:alert(1)", "/login", "/"]) {
    assert.equal(target(await middleware(req(`/login?next=${encodeURIComponent(bad)}`, c))), "/chat", bad);
  }
});

test("no redirect loops: a redirect target is never itself a signed-out-only page", async () => {
  const c = { qs_access: fresh() };
  for (const p of ["/", "/login", "/login?next=%2Flogin", "/login?next=%2F", "/signup"]) {
    const to = target(await middleware(req(p, c)));
    const second = await middleware(req(to, c));
    assert.ok(isNext(second), `${p} -> ${to} must not redirect again`);
  }
});

test("?expired=1 (forced logout / logout failure) shows the page and clears cookies instead of bouncing", async () => {
  for (const p of ["/?expired=1", "/login?expired=1"]) {
    const res = await middleware(req(p, { qs_access: fresh(), qs_refresh: "R" }));
    assert.ok(isNext(res), p);
    assert.deepEqual(cleared(res).sort(), ["qs_access", "qs_refresh"]);
  }
});

test("after logout (cookies gone) / stays on the homepage; a protected path still goes to /login?next=", async () => {
  assert.ok(isNext(await middleware(req("/"))));
  assert.equal(target(await middleware(req("/charts"))), "/login?next=%2Fcharts");
  assert.equal(target(await middleware(req("/charts/3?tab=evaluate"))), "/login?next=%2Fcharts%2F3%3Ftab%3Devaluate");
});
