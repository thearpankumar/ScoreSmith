// Two roles: normal users see only Charts / Chat / Evaluations; admin pages are blocked for them by the middleware
// (the server-rendered pages and the API re-check authoritatively).
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

register("./fixtures/alias-loader.mjs", import.meta.url);
const { isAdminOnlyPath, jwtStringClaim, roleMayOpen } = await import("../lib/auth-helpers.ts");
const { navItemsFor, NAV_ITEMS } = await import("../components/layout/nav-items.ts");
const { middleware } = await import("../middleware.ts");
const { NextRequest } = await import("next/server");

const jwt = (claims) => `x.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.y`;
const now = () => Math.floor(Date.now() / 1000);
const token = (role) => jwt({ exp: now() + 600, ...(role ? { rol: role } : {}) });
const req = (path, access) => new NextRequest(`http://localhost:3000${path}`, { headers: { cookie: `qs_access=${access}` } });
const isNext = (res) => res.headers.get("x-middleware-next") === "1";

test("navigation: a normal user sees exactly Charts, Chat and Evaluations; an admin also Settings and Users", () => {
  assert.deepEqual(navItemsFor("user").map((i) => i.label).sort(), ["Charts", "Chat", "Evaluations"]);
  assert.deepEqual(navItemsFor(undefined).map((i) => i.label).sort(), ["Charts", "Chat", "Evaluations"]);
  assert.deepEqual(navItemsFor("admin").map((i) => i.label), ["Chat", "Charts", "Evaluations", "Settings", "Users"]);
  assert.equal(NAV_ITEMS.some((i) => i.href.startsWith("/settings") || i.href.startsWith("/admin")), false);
});

test("admin-only path detection covers the whole subtree and nothing else", () => {
  for (const p of ["/admin", "/admin/users", "/admin/users/123", "/settings", "/settings/profile"]) assert.equal(isAdminOnlyPath(p), true, p);
  for (const p of ["/chat", "/charts", "/charts/abc", "/evaluations", "/administrator", "/settingsx"]) assert.equal(isAdminOnlyPath(p), false, p);
});

test("role claim helpers", () => {
  assert.equal(jwtStringClaim(token("admin"), "rol"), "admin");
  assert.equal(jwtStringClaim(token(null), "rol"), null);
  assert.equal(jwtStringClaim("garbage", "rol"), null);
  assert.equal(roleMayOpen("/admin/users", "user"), false);
  assert.equal(roleMayOpen("/admin/users", "admin"), true);
  assert.equal(roleMayOpen("/admin/users", null), true); // unknown role: the page / API decide
  assert.equal(roleMayOpen("/chat", "user"), true);
});

test("middleware redirects a normal user away from admin pages but lets admins in", async () => {
  for (const path of ["/admin/users", "/settings"]) {
    const res = await middleware(req(path, token("user")));
    assert.equal(res.status, 307, path);
    assert.equal(new URL(res.headers.get("location")).pathname, "/chat");
    assert.ok(isNext(await middleware(req(path, token("admin")))), path);
  }
  assert.ok(isNext(await middleware(req("/charts", token("user")))));
  assert.ok(isNext(await middleware(req("/evaluations", token("user")))));
});
