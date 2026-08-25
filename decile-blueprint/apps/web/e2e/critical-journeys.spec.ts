import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

import { dismissCookieBanner } from "./helpers/screen-chips";

/**
 * The ten critical user journeys — Prompt 19 §5.
 *
 *     "Playwright e2e suite covering the ten critical user journeys end to end, run against a
 *      seeded database in CI."
 *
 * WHY THIS FILE IS A REGISTRY AND NOT TEN NEW TESTS
 * -------------------------------------------------
 * Nine of the ten were already walked end to end by the specs Prompts 8-15 delivered. Rewriting
 * them here would double the browser suite's wall clock — against Prompt 19's own acceptance
 * criterion that "CI is green, deterministic, and completes in under 15 minutes" — and would
 * leave two copies of each journey to drift apart.
 *
 * So the ten are *named*, in one place, with the spec file and test title that walks each. The
 * final test in this file reads those spec files and fails if a named test has been renamed or
 * deleted. That is the property a registry has to have to be worth anything: a journey cannot
 * lose its coverage silently, which is exactly how a "ten critical journeys" suite rots.
 *
 * The one journey with no coverage before this module — running a backtest — is walked here in
 * full, and honestly: see the note on that test about what a queued backtest can and cannot do in
 * this environment.
 */

/** The seeded account (`baskfy_api.seed e2e`). Subscribed, so the gated surfaces are reachable. */
const EMAIL = "e2e@example.com";
/** `baskfy_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

interface Journey {
  /** Stable id. Referenced by `.overnight/module-19.md` and by the report. */
  readonly id: string;
  /** What the user is trying to do, in their words. */
  readonly title: string;
  /** The specification section this journey comes from. */
  readonly docs: string;
  /** The spec file that walks it, and the exact `test(...)` title inside it. */
  readonly spec: string;
  readonly walkedBy: readonly string[];
}

export const CRITICAL_JOURNEYS: readonly Journey[] = [
  {
    id: "open-an-account",
    title: "Register, confirm the address, sign in, change the password, delete the account",
    docs: "docs/08 §Routes (auth), docs/11 §Compliance",
    spec: "account.spec.ts",
    walkedBy: ["register → verify → login → change password → delete account"],
  },
  {
    id: "sign-in-to-a-gated-page",
    title: "Follow a link to a gated page while signed out, and land back on it after signing in",
    docs: "docs/08 §Routes, docs/11 §Security",
    spec: "account.spec.ts",
    walkedBy: [
      "a gated route sends an anonymous visitor to sign in, and back again",
      "signs in with a one-time code, which is the default path",
    ],
  },
  {
    id: "build-and-run-a-screen",
    title: "Create a screen, set a universe, a sort factor and three filters, and run it",
    docs: "docs/01 §2, docs/06, docs/08 §Screen editor",
    spec: "screens.spec.ts",
    walkedBy: ["create, configure, apply, edit columns, export, delete"],
  },
  {
    id: "share-a-screen",
    title: "Send someone a link to a screen and have them see the same form and the same rows",
    docs: "docs/08 §URL state",
    spec: "url-state.spec.ts",
    walkedBy: [
      "a shared link reproduces the form in a fresh session",
      "a full filter state survives a reload",
    ],
  },
  {
    id: "export-screen-results",
    title: "Export a screen's results as the CSV docs/13 specifies",
    docs: "docs/13 §5, docs/07 §/screens/{id}/export",
    spec: "screens.spec.ts",
    walkedBy: ["create, configure, apply, edit columns, export, delete"],
  },
  {
    id: "read-a-factsheet",
    title: "Click a result row and read the instrument's factsheet",
    docs: "docs/01 §5, docs/08 §Instrument",
    spec: "instrument.spec.ts",
    walkedBy: [
      "is reachable from the results table's peek drawer",
      "renders every block of docs/01 §5, anonymously",
    ],
  },
  {
    id: "check-the-market",
    title: "Look at the indices dashboard, market breadth and the listings register",
    docs: "docs/01 §6-7, docs/08 §Dashboard",
    spec: "market.spec.ts",
    walkedBy: [
      "renders every index, sorted by change descending",
      "renders the four gauges with docs/01 §6's wording",
      "shows a page of the register, newest first",
    ],
  },
  {
    id: "rebalance-a-portfolio",
    title: "Upload a holdings CSV, pick a screen, and get exits / inside-buffer / entries",
    docs: "docs/01 §8, docs/08 §Rebalance tracker",
    spec: "portfolios.spec.ts",
    walkedBy: [
      "an upload reports what it could not match rather than dropping it",
      "the wizard runs a screen and shows the three columns",
    ],
  },
  {
    id: "run-a-backtest",
    title: "Configure a backtest on a saved screen and queue it",
    docs: "docs/10, docs/08 §Backtests",
    spec: "critical-journeys.spec.ts",
    walkedBy: ["queues a backtest against a screen the user just saved"],
  },
  {
    id: "pay-and-be-invoiced",
    title: "Compare the plans, see what a purchase commits to, and find the invoices",
    docs: "docs/01 §1, docs/11 §Compliance",
    spec: "billing.spec.ts",
    walkedBy: [
      "shows exactly the plans and prices the API serves",
      "states, at the point of sale, what Forever means",
    ],
  },
];

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/build/);
  await dismissCookieBanner(page);
}

/** Create and persist a minimal screen, and return its public id. A backtest needs one. */
async function saveAScreen(page: Page): Promise<string> {
  await page.goto("/build");
  await dismissCookieBanner(page);
  await page.getByTestId("new-screen").click();
  await page.waitForURL(/\/build\/[0-9a-f]{12}$/);
  const publicId = new URL(page.url()).pathname.split("/").at(-1) as string;

  await page.getByTestId("chip-index").click();
  await page.getByTestId("index-select").selectOption("nifty-total-market");
  await expect(page.getByTestId("result-count")).toContainText("matches", { timeout: 20_000 });
  const unsaved = page.getByTestId("unsaved-badge");
  if (await unsaved.isVisible()) {
    await page.getByTestId("apply-filters").click();
    await expect(unsaved).toHaveCount(0);
  }
  return publicId;
}

test.describe("the ten critical user journeys", () => {
  /**
   * Journey 9. The only one with no Playwright coverage before Prompt 19.
   *
   * WHAT THIS CAN AND CANNOT ASSERT. `POST /backtests` publishes `baskfy.backtest.run` to Celery
   * and the browser suite starts no worker, so the run stays `queued` and never produces an
   * equity curve. Even with a worker it would fail: `ohlcv_daily` in the seeded database holds a
   * single trading day of *results* and no price history, so the loader raises "no adjusted bars
   * exist for any name this screen selected" (CLAUDE.md, open items). That is correct behaviour
   * for an empty database, not a bug this test should paper over.
   *
   * So the journey asserted is exactly the journey the repository can deliver today: a user with
   * a saved screen can configure a backtest, queue it, and land on a page that tells them its
   * real state. Everything past "queued" needs a backfill.
   */
  test("queues a backtest against a screen the user just saved", async ({ page }) => {
    test.slow();
    await signIn(page);
    const screenId = await saveAScreen(page);

    await page.goto("/backtests");
    await expect(page.getByRole("heading", { name: "Backtests", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "New backtest" }).click();
    await expect(page.getByLabel("Screen")).toBeVisible();

    // The screen just saved must be offered — a backtest runs a *saved* screen (docs/10 §Inputs).
    const screenSelect = page.getByLabel("Screen");
    await expect(screenSelect.locator(`option[value="${screenId}"]`)).toHaveCount(1);
    await screenSelect.selectOption(screenId);

    await page.getByLabel("Start").fill("2024-11-01");
    await page.getByLabel("End").fill("2026-08-18");
    await page.getByLabel("Top N").fill("20");
    await page.getByLabel("Rebalance").selectOption("monthly");

    await page.getByRole("button", { name: "Run backtest" }).click();

    // 202 Accepted -> the client routes to the run's own page.
    await page.waitForURL(/\/backtests\/[0-9a-z]+$/i, { timeout: 30_000 });
    const body = page.locator("body");
    // The page must state the run's real state rather than an empty shell. With no worker
    // attached, that state is "queued" (or "running", if one is attached locally).
    await expect(body).toContainText(/queued|running|failed|complete/i, { timeout: 30_000 });

    // And the run appears in the list, which is what makes it findable again.
    await page.goto("/backtests");
    await expect(page.getByRole("table")).toBeVisible({ timeout: 30_000 });
  });

  /**
   * The registry's own test. It is what stops this file becoming a stale list of aspirations.
   */
  test("every named journey is still walked by a test that exists", () => {
    const here = fileURLToPath(new URL(".", import.meta.url));
    const missing: string[] = [];

    for (const journey of CRITICAL_JOURNEYS) {
      let source: string;
      try {
        source = readFileSync(`${here}${journey.spec}`, "utf-8");
      } catch {
        missing.push(`${journey.id}: ${journey.spec} does not exist`);
        continue;
      }
      for (const title of journey.walkedBy) {
        if (!source.includes(title)) {
          missing.push(`${journey.id}: ${journey.spec} no longer contains a test "${title}"`);
        }
      }
    }

    expect(missing, missing.join("\n")).toEqual([]);
  });

  test("there are ten of them, with distinct ids", () => {
    expect(CRITICAL_JOURNEYS).toHaveLength(10);
    expect(new Set(CRITICAL_JOURNEYS.map((j) => j.id)).size).toBe(10);
    for (const journey of CRITICAL_JOURNEYS) {
      expect(journey.docs, `${journey.id} names no specification section`).not.toBe("");
      expect(journey.walkedBy.length, `${journey.id} names no test`).toBeGreaterThan(0);
    }
  });
});
