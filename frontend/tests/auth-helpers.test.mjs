// Login-form validation, ?next= sanitising, public-path matching and JWT-expiry parsing (pure helpers).
import test from "node:test";
import assert from "node:assert/strict";

const h = await import("../lib/auth-helpers.ts");

const jwt = (payload) => `x.${Buffer.from(JSON.stringify(payload)).toString("base64url")}.y`;

test("validateEmail: required, shape, trims", () => {
  assert.equal(h.validateEmail(""), "Enter your email address.");
  assert.equal(h.validateEmail("   "), "Enter your email address.");
  for (const bad of ["plain", "a@b", "a b@c.com", "@x.com", "a@@x.com", `${"a".repeat(320)}@x.com`]) {
    assert.equal(h.validateEmail(bad), "Enter a valid email address.", bad);
  }
  for (const ok of ["a@b.co", " user.name+tag@example.com ", "UPPER@EXAMPLE.COM"]) assert.equal(h.validateEmail(ok), null, ok);
});

test("validateLoginPassword only requires something to be typed", () => {
  assert.equal(h.validateLoginPassword(""), "Enter your password.");
  assert.equal(h.validateLoginPassword("x"), null);
});

test("validateNewPassword: 12+ chars, no trivial choices, not the email", () => {
  assert.match(h.validateNewPassword("short"), /at least 12/);
  assert.match(h.validateNewPassword("x".repeat(129)), /at most 128/);
  assert.match(h.validateNewPassword("password1234"), /too easy/);
  assert.match(h.validateNewPassword("aaaaaaaaaaaaaaaa"), /too easy/);
  assert.match(h.validateNewPassword("bob.the.builder@x.com", "bob.the.builder@x.com"), /email/);
  assert.equal(h.validateNewPassword("correct horse battery staple"), null);
  assert.equal(h.validateNewPassword("a-12-char-pass"), null);
});

test("validateName", () => {
  assert.equal(h.validateName("  "), "Enter your name.");
  assert.equal(h.validateName("x".repeat(201)), "That name is too long.");
  assert.equal(h.validateName("Ada Lovelace"), null);
});

test("passwordStrength rises with length and variety and is Too short below the minimum", () => {
  assert.deepEqual(h.passwordStrength(""), { score: 0, label: "" });
  assert.equal(h.passwordStrength("abc").label, "Too short");
  const fair = h.passwordStrength("correcthorse1");
  const strong = h.passwordStrength("Correct-Horse-Battery-Staple-42!");
  assert.ok(strong.score > fair.score);
  assert.equal(strong.label, "Strong");
});

test("safeNextPath only allows same-site paths (no open redirect)", () => {
  assert.equal(h.safeNextPath("/charts/abc?tab=2"), "/charts/abc?tab=2");
  assert.equal(h.safeNextPath(encodeURIComponent("/evaluations?x=1")), "/evaluations?x=1");
  for (const evil of [
    "https://evil.example/x",
    "//evil.example",
    "/\\evil.example",
    "javascript:alert(1)",
    "evil.example",
    "/ok\nHeader: x",
    "%E0%A4%A", // malformed percent-encoding
    "/login",
    "/signup?x=1",
    "",
    null,
    undefined,
  ]) {
    assert.equal(h.safeNextPath(evil), "/chat", String(evil));
  }
  assert.equal(h.safeNextPath("https://evil.example", "/home"), "/home");
});

test("isPublicPath: sign-in pages are public, the app is not", () => {
  for (const p of ["/login", "/signup", "/setup", "/forgot-password", "/reset-password", "/verify-email", "/login/", "/"]) assert.ok(h.isPublicPath(p), p);
  for (const p of ["/chat", "/charts/1", "/settings", "/loginx", "/api/v1/me"]) assert.ok(!h.isPublicPath(p), p);
});

test("jwtExpiry / isExpired read exp without verifying and treat junk as expired", () => {
  const now = 1_800_000_000_000;
  assert.equal(h.jwtExpiry(jwt({ exp: 1_800_000_900 })), 1_800_000_900);
  assert.equal(h.jwtExpiry("garbage"), null);
  assert.equal(h.jwtExpiry(undefined), null);
  assert.equal(h.jwtExpiry(jwt({ sub: "x" })), null);
  assert.equal(h.isExpired(jwt({ exp: 1_800_000_900 }), now), false);
  assert.equal(h.isExpired(jwt({ exp: 1_800_000_005 }), now), true); // inside the 10 s skew
  assert.equal(h.isExpired(jwt({ exp: 1_700_000_000 }), now), true);
  assert.equal(h.isExpired("nope", now), true);
});

test("oauthErrorMessage maps callback error codes to friendly text", () => {
  assert.equal(h.oauthErrorMessage(null), null);
  assert.equal(h.oauthErrorMessage(""), null);
  assert.match(h.oauthErrorMessage("oauth_denied"), /cancelled/);
  assert.match(h.oauthErrorMessage("oauth_account_exists"), /already exists/);
  assert.match(h.oauthErrorMessage("something_else"), /try again/i);
});

test("validateLoginIdentifier: email only by default, a bare username only when the server enables it", () => {
  assert.equal(h.validateLoginIdentifier("admin", false), "Enter a valid email address.");
  assert.equal(h.validateLoginIdentifier("", false), "Enter your email address.");
  assert.equal(h.validateLoginIdentifier("a@b.co", false), null);
  assert.equal(h.validateLoginIdentifier("admin", true), null);
  assert.equal(h.validateLoginIdentifier("  ", true), "Enter your email address or username.");
  assert.equal(h.validateLoginIdentifier("not@valid", true), "Enter a valid email address.");
  assert.equal(h.validateLoginIdentifier("a@b.co", true), null);
});
