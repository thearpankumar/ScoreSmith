// Edge cases for the pure auth helpers + a parity check that the browser-side password rules never accept what the
// backend refuses (the server stays the source of truth; this just keeps the instant feedback honest).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const h = await import("../lib/auth-helpers.ts");

const b64u = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const jwt = (payload) => `x.${b64u(payload)}.y`;

test("safeNextPath: encoded and tricky open-redirect attempts all fall back", () => {
  for (const evil of [
    "/%2Fevil.example", // decodes to //evil.example
    "%2F%2Fevil.example",
    "/%5Cevil.example", // decodes to /\evil.example
    "/\\/evil.example",
    " /chat", // leading space
    "\t/chat",
    "/chat\r\nSet-Cookie: x=1",
    "/ch\u0000at",
    "https:/evil.example",
    "data:text/html,<script>1</script>",
    "/login/../login",
    "/signup#frag",
    "/forgot-password?next=/x",
    "/reset-password/abc",
    "/verify-email",
    "/setup",
    "%",
    "%00",
  ]) {
    assert.equal(h.safeNextPath(evil), "/chat", JSON.stringify(evil));
  }
});

test("safeNextPath keeps legitimate in-app targets, including queries, fragments and lookalike names", () => {
  for (const ok of ["/chat", "/charts/abc-123?tab=evaluate#kpi", "/evaluations?status=failed&page=2", "/loginx", "/login-help", "/settings"]) {
    assert.equal(h.safeNextPath(ok), ok, ok);
  }
  assert.equal(h.safeNextPath("/"), "/chat"); // the homepage is not an in-app target
  assert.equal(h.safeNextPath("/charts%2F42"), "/charts/42"); // single decode only
  assert.equal(h.safeNextPath(undefined, "/home"), "/home");
  assert.equal(h.safeNextPath("", "/home"), "/home");
});

test("isPublicPath matches exact pages and their sub-paths only", () => {
  for (const p of ["/login", "/login/", "/login/x", "/reset-password", "/reset-password/abc", "/"]) assert.ok(h.isPublicPath(p), p);
  for (const p of ["/log", "/login-help", "/loginx/y", "/signups", "", "/chat/login", "/api/login", "/Login"]) assert.ok(!h.isPublicPath(p), p);
});

test("validateEmail boundaries", () => {
  const local = "a".repeat(64);
  assert.equal(h.validateEmail(`${local}@example.com`), null);
  assert.equal(h.validateEmail(`${"a".repeat(315)}@x.co`), null); // exactly 320 characters
  assert.equal(h.validateEmail(`${"a".repeat(316)}@x.co`), "Enter a valid email address."); // 321
  for (const bad of ["a@b.c", "a@.com", "a@b..", "a b@x.co", "a@x .co", "a@x.co\n"]) {
    // single-letter TLD, missing label, spaces: all rejected (the trim handles only the ends)
    if (bad === "a@x.co\n") assert.equal(h.validateEmail(bad), null); // trailing whitespace is trimmed away
    else assert.equal(h.validateEmail(bad), "Enter a valid email address.", JSON.stringify(bad));
  }
});

test("validateNewPassword boundaries: 11/12 and 128/129 characters", () => {
  const strong = "aB3$xY7!qZ".repeat(13);
  assert.match(h.validateNewPassword(strong.slice(0, 11)), /at least 12/);
  assert.equal(h.validateNewPassword(strong.slice(0, 12)), null);
  assert.equal(h.validateNewPassword(strong.slice(0, 128)), null);
  assert.match(h.validateNewPassword(strong.slice(0, 129)), /at most 128/);
  assert.match(h.validateNewPassword("Password1234"), /too easy/); // case-insensitive
  assert.match(h.validateNewPassword("abababababab"), /too easy/); // fewer than five distinct characters
  assert.match(h.validateNewPassword("Bob@Example.com", " bob@example.com "), /email/);
  assert.match(h.validateNewPassword("bob-the-user", "bob-the-user@example.com"), /email/);
});

test("validateName and validateLoginPassword boundaries", () => {
  assert.equal(h.validateName("x"), null);
  assert.equal(h.validateName("x".repeat(200)), null);
  assert.equal(h.validateName(` ${"x".repeat(200)} `), null); // trimmed first
  assert.equal(h.validateName("x".repeat(201)), "That name is too long.");
  assert.equal(h.validateLoginPassword(" "), null); // a space IS a password; the server decides
});

test("passwordStrength: never 'Strong' for a rejected password, monotonic in effort", () => {
  assert.equal(h.passwordStrength("password1234").score <= 1, true);
  assert.equal(h.passwordStrength("aaaaaaaaaaaaaaaaaaaaaaaa").score <= 1, true);
  const scores = ["abc", "correcthorse1", "Correct-Horse-1", "Correct-Horse-Battery-Staple-42!"].map((p) => h.passwordStrength(p).score);
  for (let i = 1; i < scores.length; i++) assert.ok(scores[i] >= scores[i - 1], scores.join(","));
  assert.equal(h.passwordStrength("x".repeat(10)).label, "Too short");
});

test("jwtExpiry: base64url payloads, wrong shapes and junk exp values", () => {
  assert.equal(h.jwtExpiry(`x.${Buffer.from(JSON.stringify({ exp: 123 })).toString("base64url")}.y`), 123);
  // base64url characters (- and _) must decode
  const payload = { exp: 99, note: "??>>~~" };
  assert.equal(h.jwtExpiry(jwt(payload)), 99);
  assert.equal(h.jwtExpiry("a.b"), null);
  assert.equal(h.jwtExpiry("a.b.c.d"), null);
  assert.equal(h.jwtExpiry(jwt({ exp: "123" })), null); // string exp is not trusted
  assert.equal(h.jwtExpiry(jwt({ exp: null })), null);
  assert.equal(h.jwtExpiry("x.!!!.y"), null);
  assert.equal(h.jwtExpiry(`x.${Buffer.from("not json").toString("base64url")}.y`), null);
  assert.equal(h.jwtExpiry(""), null);
});

test("isExpired: the 10 second skew is inclusive and configurable", () => {
  const now = 1_800_000_000_000;
  const at = (offsetSeconds) => jwt({ exp: now / 1000 + offsetSeconds });
  assert.equal(h.isExpired(at(10), now), true); // exactly at the skew edge
  assert.equal(h.isExpired(at(11), now), false);
  assert.equal(h.isExpired(at(11), now, 30), true);
  assert.equal(h.isExpired(at(-1), now, 0), true);
  assert.equal(h.isExpired(at(1), now, 0), false);
  assert.equal(h.isExpired(undefined, now), true);
  assert.equal(h.isExpired(null, now), true);
});

test("oauthErrorMessage covers every code the backend redirects with", () => {
  const backendCodes = ["oauth_denied", "oauth_state", "oauth_no_email", "oauth_account_exists", "oauth_signup_disabled", "oauth_disabled"];
  const fallback = h.oauthErrorMessage("totally_unknown");
  for (const code of backendCodes) {
    const msg = h.oauthErrorMessage(code);
    assert.ok(msg && msg !== fallback, `${code} needs its own message`);
  }
});

test("every error code the backend OAuth callback can emit is known to the frontend", () => {
  const src = readFileSync(new URL("../../backend/app/api/v1/oauth.py", import.meta.url), "utf8");
  const codes = new Set([...src.matchAll(/_fail\(response, "(oauth_[a-z_]+)"/g)].map((m) => m[1]));
  assert.ok(codes.size > 0, "found no error codes in oauth.py");
  const fallback = h.oauthErrorMessage("totally_unknown");
  for (const code of codes) {
    if (code === "oauth_failed") assert.equal(h.oauthErrorMessage(code), fallback); // the generic code uses the generic text
    else assert.notEqual(h.oauthErrorMessage(code), fallback, `${code} has no friendly message`);
  }
});

test("password rules match the backend: lengths and the common-password list", () => {
  const cfg = readFileSync(new URL("../../backend/app/config.py", import.meta.url), "utf8");
  assert.equal(Number(cfg.match(/password_min_length:\s*int\s*=\s*(\d+)/)[1]), h.PASSWORD_MIN_LENGTH);
  assert.equal(Number(cfg.match(/password_max_length:\s*int\s*=\s*(\d+)/)[1]), h.PASSWORD_MAX_LENGTH);
  const sec = readFileSync(new URL("../../backend/app/auth/security.py", import.meta.url), "utf8");
  const block = sec.match(/_COMMON = frozenset\(([\s\S]*?)\.split\(\)\s*\)/)[1];
  const words = [...block.matchAll(/"([^"]+)"/g)].flatMap((m) => m[1].split(/\s+/)).filter(Boolean);
  assert.ok(words.length >= 10, "failed to parse the backend list");
  for (const w of words.filter((x) => x.length >= h.PASSWORD_MIN_LENGTH)) {
    assert.match(h.validateNewPassword(w) ?? "", /too easy/, `frontend accepts '${w}', which the backend rejects`);
  }
});
