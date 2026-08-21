import { expect, test, type Page } from "@playwright/test";

import { DISCLAIMER_LABEL, DISCLAIMER_TEXT } from "../src/lib/disclaimer-text";
import { NAV_ITEMS } from "../src/lib/nav";
import { CONTENT_ROUTES, LEGAL_ROUTES } from "../src/lib/marketing/routes";

/**
 * Prompt 18's second acceptance criterion:
 *
 *     "The Disclaimer component appears on every analytics route (assert with a Playwright sweep
 *      of the route table)."
 *
 * and docs/11 §"Compliance & legal (India)": "A `<Disclaimer/>` component on every analytics
 * surface, in the footer, and on checkout."
 *
 * ## The route table is derived, not retyped
 *
 * The swept set is built from `src/lib/nav.ts` — the sidebar's own IA, which docs/08 §"App shell"
 * pins and which a test already checks against that document — plus the parameterised routes a nav
 * item cannot name, plus the public content and legal routes. A hand-written list here would go
 * stale the first time a route was added, and a sweep that silently stops covering a page is worse
 * than no sweep.
 *
 * ## What "appears" means
 *
 * The landmark is located by its accessible name and its text is compared **verbatim** against
 * `DISCLAIMER_TEXT`. Checking only for presence would pass a page that rendered an empty `<aside>`;
 * checking for a substring would pass a page that had quietly reworded the regulatory sentence.
 */

const EMAIL = "e2e@example.com";
/** `decile_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

/** A symbol the seeded reference export contains; docs/13 works the whole spec through it. */
const SAMPLE_SYMBOL = "CUPID";

/**
 * Every route the disclaimer must appear on.
 *
 * `analytics: true` means docs/11's "every analytics surface" — a page that shows a computed
 * figure about a security. `analytics: false` marks the two other places the document names (the
 * footer, and checkout) plus the public content pages, which carry it through `SiteFooter`.
 */
interface SweptRoute {
  path: string;
  name: string;
  analytics: boolean;
  requiresAuth: boolean;
}

/** The sidebar's ready destinations. `/pricing` and the account pages come with it. */
const NAV_ROUTES: SweptRoute[] = NAV_ITEMS.filter((item) => item.status === "ready").map(
  (item) => ({
    path: item.href,
    name: `nav: ${item.label}`,
    // Every one of these renders inside `AppShell`, which is where the component lives.
    analytics: !["/profile", "/change-password", "/invoices"].includes(item.href),
    requiresAuth: true,
  }),
);

/** Routes with a parameter in them: no nav item names these, and they are the densest pages. */
const PARAMETERISED: SweptRoute[] = [
  {
    path: `/instruments/${SAMPLE_SYMBOL}`,
    name: "instrument factsheet",
    analytics: true,
    requiresAuth: false,
  },
];

/** The public surface Prompt 18 delivers. It carries the disclaimer through `SiteFooter`. */
const PUBLIC: SweptRoute[] = [
  { path: "/", name: "landing", analytics: false, requiresAuth: false },
  ...CONTENT_ROUTES.map((route) => ({
    path: route.href,
    name: `content: ${route.label}`,
    analytics: false,
    requiresAuth: false,
  })),
  ...LEGAL_ROUTES.map((route) => ({
    path: route.href,
    name: `legal: ${route.label}`,
    analytics: false,
    requiresAuth: false,
  })),
];

const ROUTES: SweptRoute[] = [...PUBLIC, ...PARAMETERISED, ...NAV_ROUTES];

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/screens/);
}

async function assertDisclaimer(page: Page, path: string): Promise<void> {
  const disclaimer = page.getByRole("complementary", { name: DISCLAIMER_LABEL });
  await expect(disclaimer.first(), `no disclaimer on ${path}`).toBeVisible();
  /* Verbatim. A reworded regulatory statement is the failure this exists to catch. */
  await expect(disclaimer.first()).toHaveText(DISCLAIMER_TEXT);
}

test.describe("the public routes carry the disclaimer without an account", () => {
  for (const route of ROUTES.filter((candidate) => !candidate.requiresAuth)) {
    test(`${route.name} (${route.path})`, async ({ page }) => {
      const response = await page.goto(route.path);
      expect(response?.status(), `${route.path} did not render`).toBeLessThan(400);
      await assertDisclaimer(page, route.path);
    });
  }
});

test.describe("every signed-in route carries the disclaimer", () => {
  test.describe.configure({ mode: "serial" });

  test("sweeps the whole route table", async ({ page }) => {
    await signIn(page);
    const missing: string[] = [];

    for (const route of ROUTES.filter((candidate) => candidate.requiresAuth)) {
      const response = await page.goto(route.path);
      if ((response?.status() ?? 500) >= 400) {
        missing.push(`${route.path} responded ${response?.status()}`);
        continue;
      }
      const disclaimer = page.getByRole("complementary", { name: DISCLAIMER_LABEL }).first();
      /* Collected rather than asserted one at a time: a sweep that stops at the first failure
         tells you about one route when you want the list. */
      if ((await disclaimer.count()) === 0) {
        missing.push(`${route.path} has no disclaimer`);
        continue;
      }
      const text = (await disclaimer.textContent())?.trim();
      if (text !== DISCLAIMER_TEXT) missing.push(`${route.path} has reworded it: ${text}`);
    }

    expect(missing.join("\n")).toBe("");
  });
});

test("the route table is not empty and covers the analytics surfaces", () => {
  /* A sweep over an empty list passes silently. This is the guard on the guard. */
  expect(ROUTES.length).toBeGreaterThan(15);
  const analytics = ROUTES.filter((route) => route.analytics).map((route) => route.path);
  for (const required of [
    "/dashboard",
    "/market-health",
    "/screens",
    "/listings",
    "/backtests",
    "/portfolios",
    `/instruments/${SAMPLE_SYMBOL}`,
  ]) {
    expect(analytics, `${required} is not in the swept analytics set`).toContain(required);
  }
});
