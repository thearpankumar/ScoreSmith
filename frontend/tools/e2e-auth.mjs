// End-to-end smoke of the sign-in flow in a real browser against a running stack (frontend + backend). DEV TOOL ONLY.
//   node tools/e2e-auth.mjs [--url http://localhost:3100]
// Creates a throwaway account through the UI, then checks: protected routes redirect to /login, cookie flags,
// wrong-password message, successful sign-in, session survives navigation, silent renewal of an expired access
// token, sign-out, and that the old session no longer works.
import { chromium } from "playwright";

const base = process.argv.includes("--url") ? process.argv[process.argv.indexOf("--url") + 1] : "http://localhost:3100";
const email = `e2e-${Date.now()}@example.com`;
const password = process.env.E2E_PASSWORD || `e2e-${crypto.randomUUID()}-Aa1!`; // never a committed secret
let failed = 0;
const check = (ok, label, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) failed++;
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1672, height: 941 } });
const page = await ctx.newPage();

// 1. signed out: a protected page bounces to /login?next=...
await page.goto(`${base}/charts`, { waitUntil: "networkidle" });
check(new URL(page.url()).pathname === "/login", "signed-out visit to /charts redirects to /login", page.url());
check(new URL(page.url()).searchParams.get("next") === "/charts", "redirect keeps ?next=/charts");

// 2. sign up through the UI
await page.goto(`${base}/signup`, { waitUntil: "networkidle" });
await page.fill("#signup-name", "E2E Tester");
await page.fill("#signup-email", email);
await page.fill("#signup-password", password);
await page.click("button.auth-submit");
await page.waitForURL(/\/chat/, { timeout: 30000 });
check(true, "signup lands on /chat", page.url());

// 3. cookie flags
const cookies = await ctx.cookies();
const by = Object.fromEntries(cookies.map((c) => [c.name, c]));
check(by.qs_access?.httpOnly && by.qs_refresh?.httpOnly, "access + refresh cookies are httpOnly");
check(by.qs_access?.sameSite === "Lax" && by.qs_refresh?.sameSite === "Lax", "cookies are SameSite=Lax");
check(by.qs_csrf && !by.qs_csrf.httpOnly, "CSRF cookie is readable by the page");
check(!(await page.evaluate(() => document.cookie.includes("qs_access") || document.cookie.includes("qs_refresh"))), "JS cannot read the tokens");

// 4. signed-in app works: user menu shows the account, API calls go through the proxy
await page.waitForSelector('button[aria-label="Sign out"]');
check((await page.textContent("nav")).includes("E2E Tester"), "nav shows the signed-in user's name");
const me = await page.evaluate(() => fetch("/api/v1/me", { credentials: "include" }).then((r) => r.json()));
check(me.email === email, "GET /api/v1/me through the proxy returns the account");

// 5. navigation keeps the session (let the chat page finish its own redirect first)
await page.waitForLoadState("networkidle");
await page.waitForTimeout(1500);
await page.goto(`${base}/settings`, { waitUntil: "networkidle" });
check(new URL(page.url()).pathname === "/settings", "navigating to /settings stays signed in");

// 6. silent renewal: delete the access cookie (as if it expired); the middleware renews it from the refresh cookie
const before = by.qs_refresh.value;
await ctx.clearCookies({ name: "qs_access" });
await page.goto(`${base}/evaluations`, { waitUntil: "networkidle" });
check(new URL(page.url()).pathname === "/evaluations", "expired access token is renewed silently");
const after = (await ctx.cookies()).find((c) => c.name === "qs_refresh")?.value;
check(after && after !== before, "refresh token rotated on renewal");

// 7. sign out
await page.click('button[aria-label="Sign out"]');
await page.waitForURL(/\/login/, { timeout: 30000 });
check(true, "sign-out returns to /login");
check(!(await ctx.cookies()).some((c) => c.name === "qs_access" && c.value), "access cookie cleared");
await page.goto(`${base}/chat`, { waitUntil: "networkidle" });
check(new URL(page.url()).pathname === "/login", "after sign-out /chat redirects to /login");

// 8. wrong password, then right one with Remember me
await page.goto(`${base}/login`, { waitUntil: "networkidle" });
await page.fill("#login-email", email);
await page.fill("#login-password", "definitely-the-wrong-password");
await page.click("button.auth-submit");
await page.waitForSelector(".auth-status[data-tone=error]:not(:empty)");
check((await page.textContent(".auth-status")).includes("Incorrect email or password"), "wrong password shows a generic error");
await page.fill("#login-password", password);
await page.check(".auth-check input");
await page.click("button.auth-submit");
await page.waitForURL(/\/chat/, { timeout: 30000 });
const refresh = (await ctx.cookies()).find((c) => c.name === "qs_refresh");
check(refresh && refresh.expires > Date.now() / 1000 + 25 * 86400, "Remember me makes the refresh cookie persistent (~30 days)");

// 9. open redirect is refused
await ctx.clearCookies();
await page.goto(`${base}/login?next=${encodeURIComponent("https://evil.example/x")}`, { waitUntil: "networkidle" });
await page.fill("#login-email", email);
await page.fill("#login-password", password);
await page.click("button.auth-submit");
await page.waitForURL(/\/chat/, { timeout: 30000 });
check(new URL(page.url()).origin === new URL(base).origin, "?next=https://evil.example is ignored");

// 10. OAuth buttons are inert with a clear message
await ctx.clearCookies();
await page.goto(`${base}/login`, { waitUntil: "networkidle" });
await page.click('button[aria-label^="Continue with Google"]', { force: true }); // aria-disabled: pressing it must explain, not navigate
check((await page.textContent(".auth-divider-text")).includes("isn’t set up"), "unconfigured OAuth button explains itself instead of navigating");

await browser.close();
console.log(failed ? `\n${failed} check(s) FAILED` : "\nall checks passed");
process.exit(failed ? 1 : 0);
