/**
 * Full-route UI sweep against a running dev server.
 *
 * Visits EVERY page route in the app with an authenticated session and records, per route:
 * HTTP status, whether Next's error overlay / error boundary rendered, browser console errors,
 * uncaught page exceptions, unhandled promise rejections, and failed network requests.
 *
 * Written as a standalone script rather than a Playwright spec so it can run against the ordinary
 * dev server on :3000 — the same server the developer is looking at — instead of the e2e harness's
 * isolated stack on :3100.
 */
import { chromium } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { URL } from "node:url";

const BASE = process.env.SWEEP_BASE ?? "http://localhost:3000";
const EMAIL = "e2e@example.com";
const PASSWORD = "e2e-suite-password";
const OUT = process.env.SWEEP_OUT ?? "/tmp/route-probe.txt";

/** Every page route, with dynamic segments filled from real seeded rows. */
const ROUTES = [
  // marketing / public
  "/", "/about", "/blog", "/blog/what-a-decile-actually-measures",
  "/december-2026-update", "/disclaimer", "/faq", "/privacy-policy",
  "/refund-policy", "/support", "/terms-conditions", "/pricing",
  // auth
  "/login", "/register", "/forgot-password", "/reset-password", "/verify-email",
  // market hub (tree-6 IA)
  "/market", "/market/today", "/market/mood", "/market/listings",
  // baskets hub
  "/baskets", "/baskets/featured", "/baskets/plan",
  "/basket/momentum-scan", "/basket/momentum-scan/constituents",
  // build hub
  "/build", "/build/new", "/build/exmpl0000001", "/build/exmpl0000001/columns",
  "/build/backtests", "/build/backtests/e167363332814ee574c2521f",
  // me hub
  "/me", "/me/investments", "/me/portfolios", "/me/watchlist",
  "/me/investments/1", "/me/investments/1/customize", "/me/investments/1/orders",
  // instruments
  "/instruments", "/instruments/RELIANCE",
  // account / ops surfaces
  "/profile", "/change-password", "/api-keys", "/alerts", "/alerts/unsubscribe",
  "/brokers", "/holdings", "/fees", "/invoices", "/create",
  "/performance", "/reconcile", "/regime", "/tradebook", "/kitchen-sink",
  "/portfolios/3/rebalance", "/portfolios/3/sleeves",
  // admin
  "/admin", "/admin/pipeline", "/admin/public-api", "/admin/users",
  // legacy paths that must redirect, not 404 or 500
  "/dashboard", "/market-health", "/listings", "/explore", "/screens",
  "/screens/exmpl0000001", "/screens/exmpl0000001/columns",
  "/backtests", "/backtests/e167363332814ee574c2521f",
  "/investments", "/investments/1", "/investments/1/customize", "/investments/1/orders",
  "/portfolios", "/watchlist",
];

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });

// --- sign in once, reuse the session cookie for the whole sweep -------------------------------
{
  const page = await context.newPage();
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded", timeout: 90_000 });
  await page.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});
  /*
   * OTP is the default path (docs/11 §Security); the password form lives behind its own tab.
   * The click is retried because a click dispatched before React hydrates is silently dropped —
   * the tab is in the HTML long before its handler is attached, so a single click "succeeds"
   * and changes nothing.
   */
  const passwordField = page.getByLabel("Password");
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await page.getByRole("tab", { name: "Password" }).click().catch(() => {});
    if (await passwordField.isVisible().catch(() => false)) break;
    await page.waitForTimeout(1_000);
  }
  if (!(await passwordField.isVisible().catch(() => false))) {
    throw new Error("the password tab never revealed its form — login page did not hydrate");
  }
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 90_000 });
  // The cookie banner is fixed to the bottom and overlaps controls on every subsequent page.
  const essential = page.getByRole("button", { name: "Essential only" });
  if (await essential.isVisible().catch(() => false)) await essential.click();
  console.log(`signed in -> ${page.url()}`);
  await page.close();
}

const lines = [];
let idx = 0;
for (const route of ROUTES) {
  idx += 1;
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const rejections = [];
  const failedRequests = [];

  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text().replace(/\s+/g, " ").slice(0, 300));
  });
  page.on("pageerror", (err) => pageErrors.push(String(err.message ?? err).replace(/\s+/g, " ").slice(0, 300)));
  page.on("requestfailed", (req) => {
    const f = req.failure()?.errorText ?? "failed";
    if (f !== "net::ERR_ABORTED") failedRequests.push(`${f} ${req.url().replace(BASE, "").slice(0, 120)}`);
  });

  // Surface unhandled promise rejections the way the Next dev overlay does.
  await page.addInitScript(() => {
    globalThis.addEventListener("unhandledrejection", (event) => {
      const r = event.reason;
      const described =
        r instanceof Error ? `${r.name}: ${r.message}`
        : typeof r === "object" && r !== null ? `${Object.prototype.toString.call(r)}${r.type ? ` type=${r.type}` : ""}${r.target?.src ? ` src=${r.target.src}` : ""}`
        : String(r);
      (globalThis.__sweepRejections ??= []).push(described);
    });
  });

  let status = 0;
  let finalUrl = route;
  let overlay = "";
  let bodyText = "";
  try {
    const res = await page.goto(`${BASE}${route}`, { waitUntil: "domcontentloaded", timeout: 90_000 });
    status = res?.status() ?? 0;
    await page.waitForLoadState("networkidle", { timeout: 20_000 }).catch(() => {});
    finalUrl = new URL(page.url()).pathname + new URL(page.url()).search;
    bodyText = (await page.locator("body").innerText().catch(() => "")).replace(/\s+/g, " ");
    // Next's dev error overlay and the app's own error boundaries.
    const overlayEl = page.locator("nextjs-portal, [data-nextjs-dialog], #__next-build-watcher");
    if (await overlayEl.count()) {
      const t = await overlayEl.first().innerText().catch(() => "");
      if (t.trim()) overlay = t.replace(/\s+/g, " ").slice(0, 300);
    }
    rejections.push(...(await page.evaluate(() => globalThis.__sweepRejections ?? []).catch(() => [])));
  } catch (error) {
    overlay = `NAVIGATION_FAILED ${String(error.message ?? error).replace(/\s+/g, " ").slice(0, 200)}`;
  }

  const boundary =
    /Application error: a server-side exception|Something went wrong|Runtime TypeError|Runtime Error|Unhandled Runtime Error|__webpack_modules__/i.test(
      `${bodyText} ${overlay}`,
    );

  const problems = [];
  if (status >= 500) problems.push(`HTTP_${status}`);
  if (status === 404) problems.push("HTTP_404");
  if (boundary) problems.push("ERROR_BOUNDARY");
  if (overlay) problems.push(`OVERLAY[${overlay}]`);
  for (const e of pageErrors) problems.push(`PAGEERROR[${e}]`);
  for (const e of rejections) problems.push(`REJECTION[${e}]`);
  for (const e of consoleErrors) problems.push(`CONSOLE[${e}]`);
  for (const e of failedRequests) problems.push(`REQFAIL[${e}]`);

  const verdict = problems.length ? "ERROR" : "ok";
  const redir = finalUrl !== route ? ` -> ${finalUrl}` : "";
  const line = `${String(idx).padStart(3, "0")} ${verdict} ${status} ${route}${redir}${problems.length ? " :: " + problems.join(" | ") : ""}`;
  lines.push(line);
  console.log(line);
  await page.close();
}

writeFileSync(OUT, lines.join("\n") + "\n");
console.log(`\nrouteS probed: ${ROUTES.length}`);
console.log(`clean: ${lines.filter((l) => l.includes(" ok ")).length}`);
console.log(`problem: ${lines.filter((l) => l.includes(" ERROR ")).length}`);
await browser.close();
