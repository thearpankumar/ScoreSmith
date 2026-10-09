// Regression: the middleware's silent session renewal must send the browser-facing Origin, not the container bind
// address (http://0.0.0.0:3000), or the backend's CSRF origin check answers 403 and the user is logged out on expiry.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const { browserOrigin } = await import("../lib/auth-helpers.ts");
const hdr = (o) => ({ get: (k) => o[k.toLowerCase()] ?? null });

test("browserOrigin prefers the Host header over the bind address", () => {
  assert.equal(browserOrigin(hdr({ host: "localhost:3000" }), { host: "0.0.0.0:3000", protocol: "http:" }), "http://localhost:3000");
});
test("browserOrigin honours forwarded host/proto from a proxy", () => {
  assert.equal(
    browserOrigin(hdr({ host: "internal:3000", "x-forwarded-host": "app.example.com", "x-forwarded-proto": "https, http" }), { host: "0.0.0.0:3000", protocol: "http:" }),
    "https://app.example.com",
  );
});
test("browserOrigin falls back to the URL when no headers exist", () => {
  assert.equal(browserOrigin(hdr({}), { host: "h:1", protocol: "https:" }), "https://h:1");
});
test("middleware does not send nextUrl.origin as the Origin header", () => {
  const src = readFileSync(new URL("../middleware.ts", import.meta.url), "utf8");
  assert.ok(!/origin:\s*req\.nextUrl\.origin/.test(src));
  assert.ok(/browserOrigin\(/.test(src));
});
