// The sign-in API wrappers (lib/auth-client.ts): what each sends, how errors surface, and the soft-failing config.
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

register("./fixtures/alias-loader.mjs", import.meta.url); // lib/auth-client.ts imports "./api-client" without an extension

const assigned = [];
globalThis.window = { location: { pathname: "/login", search: "", assign: (u) => assigned.push(u) } };
globalThis.document = { cookie: "qs_csrf=tok%2Fen", visibilityState: "visible", addEventListener() {}, removeEventListener() {} };

const calls = [];
const json = (status, body) => ({ ok: status < 400, status, statusText: "", text: async () => (body === undefined ? "" : JSON.stringify(body)) });
let handler = () => json(200, {});
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  return handler(String(url), init);
};

const auth = await import("../lib/auth-client.ts");

const lastBody = () => JSON.parse(calls.at(-1).init.body);

test("login trims the email only, never the password, and maps rememberMe to remember_me", async () => {
  calls.length = 0;
  handler = () => json(200, { user: { id: "1" } });
  await auth.login({ email: "  a@b.co  ", password: "  spaced pw  ", rememberMe: true });
  assert.equal(calls[0].url, "/api/v1/auth/login");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(lastBody(), { email: "a@b.co", password: "  spaced pw  ", remember_me: true });
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], "tok/en"); // URL-decoded double-submit value
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
});

test("signup trims email and name but not the password", async () => {
  calls.length = 0;
  handler = () => json(201, { user: {} });
  await auth.signup({ email: " x@y.zz ", name: "  Ada  ", password: " pw " });
  assert.deepEqual(lastBody(), { email: "x@y.zz", name: "Ada", password: " pw " });
});

test("a failed login surfaces the server message and never refreshes or redirects", async () => {
  calls.length = 0;
  assigned.length = 0;
  handler = () => json(401, { detail: "Invalid email or password." });
  await assert.rejects(auth.login({ email: "a@b.co", password: "x", rememberMe: false }), (e) => e.status === 401 && e.message === "Invalid email or password.");
  assert.equal(calls.length, 1);
  assert.equal(assigned.length, 0);
});

test("lockout (429) and validation (422) messages reach the form", async () => {
  handler = () => json(429, { detail: "Too many failed attempts. This account is temporarily locked; try again later." });
  await assert.rejects(auth.login({ email: "a@b.co", password: "x", rememberMe: false }), (e) => e.status === 429 && /locked/.test(e.message));
  handler = () => json(422, { detail: [{ loc: ["body", "email"], msg: "value is not a valid email address" }] });
  await assert.rejects(auth.signup({ email: "bad", name: "n", password: "p" }), (e) => e.status === 422 && e.message === "email: value is not a valid email address");
  handler = () => json(403, { detail: "Sign-up is disabled. Ask an administrator to create your account." });
  await assert.rejects(auth.signup({ email: "a@b.co", name: "n", password: "p" }), (e) => e.status === 403 && /disabled/.test(e.message));
});

test("forgot / reset / verify / logout hit the right routes with the right bodies", async () => {
  calls.length = 0;
  handler = () => json(200, { detail: "ok" });
  await auth.requestPasswordReset("  me@x.co ");
  assert.deepEqual([calls[0].url, lastBody()], ["/api/v1/auth/forgot-password", { email: "me@x.co" }]);
  await auth.resetPassword({ token: "t".repeat(12), password: "new password 1234" });
  assert.deepEqual([calls[1].url, lastBody()], ["/api/v1/auth/reset-password", { token: "t".repeat(12), password: "new password 1234" }]);
  await auth.verifyEmail("v".repeat(12));
  assert.deepEqual([calls[2].url, lastBody()], ["/api/v1/auth/verify-email", { token: "v".repeat(12) }]);
  await auth.logout();
  assert.equal(calls[3].url, "/api/v1/auth/logout");
  assert.equal(calls[3].init.body, undefined);
});

test("an invalid reset link (400) is reported, not retried", async () => {
  calls.length = 0;
  handler = () => json(400, { detail: "This reset link is invalid or has expired." });
  await assert.rejects(auth.resetPassword({ token: "x".repeat(12), password: "whatever password" }), (e) => e.status === 400);
  assert.equal(calls.length, 1);
});

test("listProviders unwraps the provider list", async () => {
  handler = () => json(200, { providers: [{ id: "google", name: "Google", enabled: false }] });
  assert.deepEqual(await auth.listProviders(), [{ id: "google", name: "Google", enabled: false }]);
});

test("oauthStartUrl is a same-origin path and only carries remember when asked", () => {
  assert.equal(auth.oauthStartUrl("google", false), "/api/v1/auth/oauth/google/start");
  assert.equal(auth.oauthStartUrl("github", true), "/api/v1/auth/oauth/github/start?remember=true");
});

test("getAuthConfig maps the flags and fails soft to the default state", async () => {
  handler = () => json(200, { signup_enabled: false, setup_required: true });
  assert.deepEqual(await auth.getAuthConfig(), { signupEnabled: false, setupRequired: true, usernameLogin: false });
  handler = () => json(200, { signup_enabled: true, setup_required: false, username_login: true });
  assert.deepEqual(await auth.getAuthConfig(), { signupEnabled: true, setupRequired: false, usernameLogin: true });
  handler = () => json(200, { signup_enabled: true, setup_required: false });
  assert.deepEqual(await auth.getAuthConfig(), { signupEnabled: true, setupRequired: false, usernameLogin: false });
  handler = () => json(200, {}); // missing fields: sign-up stays offered, no setup page
  assert.deepEqual(await auth.getAuthConfig(), { signupEnabled: true, setupRequired: false, usernameLogin: false });
  handler = () => json(500, { detail: "boom" });
  assert.deepEqual(await auth.getAuthConfig(), { signupEnabled: true, setupRequired: false, usernameLogin: false });
  handler = () => {
    throw new TypeError("network down");
  };
  assert.deepEqual(await auth.getAuthConfig(), { signupEnabled: true, setupRequired: false, usernameLogin: false });
});

test("getAuthConfig rethrows Next.js control-flow errors untouched", async () => {
  const sentinel = Object.assign(new Error("dynamic"), { digest: "DYNAMIC_SERVER_USAGE" });
  handler = () => {
    throw sentinel;
  };
  await assert.rejects(auth.getAuthConfig(), (e) => e === sentinel);
});

test("registerFirstAdmin sends the trimmed bootstrap token as a header, never in the body", async () => {
  calls.length = 0;
  handler = () => json(201, { email: "root@x.co" });
  await auth.registerFirstAdmin({ token: "  boot-token  ", email: " Root@X.co ", name: " Root ", password: "Aa1!long-enough-pw" });
  assert.equal(calls[0].url, "/api/v1/auth/register-user");
  assert.equal(calls[0].init.headers["X-Bootstrap-Token"], "boot-token");
  assert.deepEqual(lastBody(), { email: "Root@X.co", name: "Root", password: "Aa1!long-enough-pw" });
  assert.ok(!JSON.stringify(lastBody()).includes("boot-token"));
});

test("registerFirstAdmin: a closed endpoint (404) and a wrong token (403) surface their messages", async () => {
  handler = () => json(404, { detail: "Not found." });
  await assert.rejects(auth.registerFirstAdmin({ token: "t", email: "a@b.co", name: "n", password: "p" }), (e) => e.status === 404);
  handler = () => json(403, { detail: "Invalid bootstrap token." });
  await assert.rejects(auth.registerFirstAdmin({ token: "t", email: "a@b.co", name: "n", password: "p" }), (e) => e.status === 403 && /bootstrap/.test(e.message));
});
