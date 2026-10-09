// Homepage + routing smoke in a real browser. DEV TOOL ONLY.
//   node tools/e2e-home.mjs [--url http://localhost:3000] [--shots <dir>]
// Reads ADMIN_EMAIL / ADMIN_PASSWORD from the environment or infra/.env (never printed).
import { chromium } from "playwright";
import { readFileSync, mkdirSync } from "node:fs";

const arg = (n, d) => (process.argv.includes(n) ? process.argv[process.argv.indexOf(n) + 1] : d);
const base = arg("--url", "http://localhost:3000");
const shots = arg("--shots", "");
const env = { ...process.env };
try {
  for (const l of readFileSync(new URL("../../infra/.env", import.meta.url), "utf8").split(/\r?\n/)) {
    const m = l.match(/^(ADMIN_EMAIL|ADMIN_PASSWORD)=(.*)$/);
    if (m && !env[m[1]]) env[m[1]] = m[2].trim().replace(/^["']|["']$/g, "");
  }
} catch {}
let failed = 0;
const check = (ok, label, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) failed++;
};
const path = (p) => new URL(p.url()).pathname + new URL(p.url()).search;

const browser = await chromium.launch();
const errors = [];
async function fresh(opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1536, height: 1024 }, ...opts });
  const page = await ctx.newPage();
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));
  return { ctx, page };
}

// 1. logged out
let { ctx, page } = await fresh();
await page.goto(base + "/", { waitUntil: "networkidle" });
check(path(page) === "/", "logged out: / stays on /", path(page));
check((await page.locator("h1").count()) === 1, "exactly one h1");
check(await page.getByRole("link", { name: "Sign in" }).first().isVisible(), "Sign in visible");
check((await page.title()).includes("KPI Metrics"), "page title", await page.title());
if (shots) {
  mkdirSync(shots, { recursive: true });
  for (const [w, h] of [[1536, 1024], [1440, 900], [768, 1024], [390, 844]]) {
    await page.setViewportSize({ width: w, height: h });
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${shots}/home-${w}x${h}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1536, height: 1024 });
}
// Watch demo
await page.getByRole("button", { name: "Watch Demo" }).click();
check(await page.getByRole("dialog").isVisible(), "Watch Demo opens the dialog");
await page.keyboard.press("Escape");
check(!(await page.getByRole("dialog").isVisible()), "Escape closes the dialog");
// anchors resolve
for (const h of ["#product", "#outcomes", "#why-us"]) check((await page.locator(h).count()) === 1, `anchor ${h} exists`);
// 2. protected path
await page.goto(base + "/charts", { waitUntil: "networkidle" });
check(path(page) === "/login?next=%2Fcharts", "logged out: /charts -> /login?next=%2Fcharts", path(page));
// 3. sign in from the homepage
await page.goto(base + "/", { waitUntil: "networkidle" });
await page.getByRole("link", { name: "Sign in" }).first().click();
await page.waitForURL(/\/login/);
const idField = page.locator("input[type=email], input[autocomplete=username], input[name=email], #login-email").first();
await idField.fill(env.ADMIN_EMAIL);
await page.locator("input[type=password]").first().fill(env.ADMIN_PASSWORD);
const remember = page.getByLabel(/remember/i);
if (await remember.count()) await remember.check().catch(() => {});
await page.locator("button[type=submit]").first().click();
await page.waitForURL(/\/chat/, { timeout: 30000 });
check(path(page).startsWith("/chat"), "sign in lands in /chat", path(page));
// 4. logged in: / -> /chat, /login -> /chat, safe next honoured
await page.goto(base + "/", { waitUntil: "networkidle" });
check(path(page).startsWith("/chat"), "logged in: / -> /chat (no homepage)", path(page));
await page.goto(base + "/login", { waitUntil: "networkidle" });
check(path(page).startsWith("/chat"), "logged in: /login -> /chat", path(page));
await page.goto(base + "/login?next=%2Fcharts", { waitUntil: "networkidle" });
check(path(page) === "/charts", "logged in: /login?next=/charts -> /charts", path(page));
await page.goto(base + "/login?next=%2F%2Fevil.example", { waitUntil: "networkidle" });
check(path(page).startsWith("/chat"), "logged in: open-redirect next ignored", path(page));
// 5. expire only the access cookie
await ctx.clearCookies({ name: "qs_access" });
await page.goto(base + "/", { waitUntil: "networkidle" });
check(path(page).startsWith("/chat"), "access cookie gone, refresh present: / renews and -> /chat", path(page));
check((await ctx.cookies()).some((c) => c.name === "qs_access"), "renewed access cookie was set");
// 6. remember-me style: save state, reopen
const state = await ctx.storageState();
const re = await fresh({ storageState: state });
await re.page.goto(base + "/", { waitUntil: "networkidle" });
check(path(re.page).startsWith("/chat"), "reopened context with saved cookies: / -> /chat", path(re.page));
await re.ctx.close();
// 7. logout -> homepage and stays
await page.goto(base + "/chat", { waitUntil: "networkidle" });
await page.getByRole("button", { name: "Sign out" }).first().click();
await page.waitForURL((u) => u.pathname === "/", { timeout: 15000 });
await page.waitForTimeout(1500);
check(path(page) === "/" || path(page) === "/?expired=1", "logout ends on the homepage and stays", path(page));
check(await page.locator("h1").isVisible(), "homepage rendered after logout");
await page.goto(base + "/", { waitUntil: "networkidle" });
check(path(page) === "/", "after logout / does not bounce into /chat", path(page));
await page.goto(base + "/chat", { waitUntil: "networkidle" });
check(path(page).startsWith("/login"), "after logout /chat -> /login", path(page));
// 8. dead session (garbage refresh) -> homepage with cookies cleared
const dead = await fresh();
await dead.ctx.addCookies([
  { name: "qs_refresh", value: "garbage", url: base },
]);
await dead.page.goto(base + "/", { waitUntil: "networkidle" });
check(path(dead.page) === "/", "failed renewal: homepage shown", path(dead.page));
check(!(await dead.ctx.cookies()).some((c) => c.name === "qs_refresh"), "failed renewal clears the cookies");
const bad = errors.filter((e) => !/favicon/i.test(e));
check(bad.length === 0, "no console errors", bad.slice(0, 3).join(" | "));
await browser.close();
process.exit(failed ? 1 : 0);
