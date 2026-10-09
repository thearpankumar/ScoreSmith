// Responsive check of the homepage at many viewports: screenshots + no horizontal scroll + tap targets. DEV TOOL ONLY.
//   node tools/e2e-responsive.mjs [--url http://localhost:3000] [--shots <dir>]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const arg = (n, d) => (process.argv.includes(n) ? process.argv[process.argv.indexOf(n) + 1] : d);
const base = arg("--url", "http://localhost:3000");
const shots = arg("--shots", "");
const sizes = [[320, 640], [360, 740], [390, 844], [430, 932], [768, 1024], [1024, 768], [1280, 800], [1536, 1024], [1920, 1080], [844, 390]];
if (shots) mkdirSync(shots, { recursive: true });
const browser = await chromium.launch();
let failed = 0;
for (const [w, h] of sizes) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, isMobile: w < 800, hasTouch: w < 800 });
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => m.type() === "error" && errs.push(m.text()));
  await page.goto(base + "/", { waitUntil: "networkidle" });
  const r = await page.evaluate(() => {
    const de = document.scrollingElement;
    const small = [...document.querySelectorAll("a, button")]
      .filter((e) => e.offsetParent !== null && !e.closest("[hidden]") && !e.classList.contains("mk-skip"))
      .map((e) => [e.textContent.trim().slice(0, 18) || e.getAttribute("aria-label"), e.getBoundingClientRect()])
      .filter(([, b]) => b.height < 43.5 && b.width > 0)
      .map(([t, b]) => `${t}:${Math.round(b.height)}`);
    return { sw: de.scrollWidth, iw: innerWidth, small };
  });
  const ok = r.sw <= r.iw && errs.length === 0;
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"} ${w}x${h} scrollWidth=${r.sw} innerWidth=${r.iw} smallTargets=[${r.small.join(", ")}] errors=${errs.length}`);
  if (shots) await page.screenshot({ path: `${shots}/r-${w}x${h}.png`, fullPage: true });
  if (w < 900 && w > 300) {
    await page.getByRole("button", { name: "Open menu" }).click();
    if (shots && (w === 390 || w === 768)) await page.screenshot({ path: `${shots}/r-menu-${w}.png` });
    await page.keyboard.press("Escape");
    const closed = await page.locator("#mk-menu").isHidden();
    if (!closed) { failed++; console.log("FAIL menu did not close on Escape"); }
    await page.getByRole("button", { name: "Open menu" }).click();
    await page.getByRole("link", { name: "Use Cases" }).last().click();
    if (!(await page.locator("#mk-menu").isHidden())) { failed++; console.log("FAIL menu did not close on link click"); }
  }
  await ctx.close();
}
await browser.close();
process.exit(failed ? 1 : 0);
