import { expect, test, type Page } from "@playwright/test";

import { dismissCookieBanner, showResultsTable } from "./helpers/screen-chips";

/**
 * `/instruments/[symbol]` end to end — Prompt 10 deliverables 4, 5 and 6.
 *
 * The numbers asserted here come from the committed reference export
 * (`fixtures/reference-screen-export-2026-08-18.csv`), which is what the e2e database is seeded
 * from. A regression in the factsheet changes the page, not the expectation.
 *
 * The page is anonymous on purpose: docs/08 §Routes calls this "the organic-traffic surface", so
 * a crawler — and a reader arriving from search — must see the whole thing without an account.
 */

const SYMBOL = "CUPID";

/** The seeded account (`baskfy_api.seed e2e`) — the screens surface needs one. */
const EMAIL = "e2e@example.com";
/** `baskfy_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/onboarding/);
  await dismissCookieBanner(page);
}

test.describe("the instrument factsheet", () => {
  test("renders every block of docs/01 §5, anonymously", async ({ page }) => {
    await page.goto(`/instruments/${SYMBOL}`);

    await expect(page.getByRole("heading", { level: 1, name: SYMBOL })).toBeVisible();
    await expect(page.getByText("NSE: CUPID")).toBeVisible();
    await expect(page.getByText("₹284.03")).toBeVisible();

    for (const heading of [
      "Key stats",
      "Pros and cons",
      "Metrics",
      "Price & moving averages",
      "Returns, Sharpe, volatility and RSI",
      "Market quality",
      "Corporate actions",
    ]) {
      await expect(page.getByRole("heading", { level: 2, name: heading })).toBeVisible();
    }

    // docs/05 §10, from the export's own inputs: 284.03 / 299.00 - 1 = -5.01%.
    await expect(page.getByText("-5.01%").first()).toBeVisible();
    // docs/01 §5 block 3: the reference product's first PRO line, word for word.
    await expect(page.getByText("The close is above 200-day moving average.")).toBeVisible();
    // Every analytics surface carries it exactly once (docs/11 §"Compliance & legal (India)",
    // CLAUDE.md house rule 9). The app shell is what renders it.
    await expect(page.getByRole("complementary", { name: /disclaimer/i })).toHaveCount(1);
  });

  test("names the universe its percentile bars are measured against", async ({ page }) => {
    await page.goto(`/instruments/${SYMBOL}`);
    await expect(page.getByText(/^Ranked against /)).toBeVisible();
  });

  test("server-renders the SEO title, description and JSON-LD", async ({ page }) => {
    const response = await page.goto(`/instruments/${SYMBOL}`);
    const html = (await response?.text()) ?? "";

    // docs/08 §"Instrument factsheet", verbatim.
    expect(html).toContain("CUPID share price, momentum &amp; factor analysis");
    await expect(page).toHaveTitle(/CUPID share price, momentum & factor analysis/);

    const description = page.locator('meta[name="description"]');
    await expect(description).toHaveAttribute("content", /factor snapshot/);

    // In the HTML, not injected after hydration — a crawler that runs no JavaScript still gets it.
    expect(html).toContain('type="application/ld+json"');
    expect(html).toContain('"@type":"Dataset"');
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
      "href",
      /\/instruments\/CUPID$/,
    );
  });

  test("serves a per-instrument OG image", async ({ page, request, baseURL }) => {
    await page.goto(`/instruments/${SYMBOL}`);
    const advertised = await page.locator('meta[property="og:image"]').getAttribute("content");
    expect(advertised, "the route must advertise an OG image").toBeTruthy();

    // The tag carries the absolute production URL (`metadataBase`), which is right for a crawler
    // and unreachable from here; the path is what this server serves.
    const { pathname, search } = new URL(advertised ?? "");
    const image = await request.get(`${baseURL}${pathname}${search}`);
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toContain("image/png");
  });

  test("404s for a symbol that does not exist", async ({ page }) => {
    const response = await page.goto("/instruments/NOSUCHSYMBOL");
    expect(response?.status()).toBe(404);
  });

  test("is reachable from the results table's peek drawer", async ({ page }) => {
    test.slow();
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });
    await showResultsTable(page);

    await page.locator('[role="row"][aria-rowindex="2"]').click();
    const link = page.getByRole("link", { name: /Open the full factsheet/ });
    await expect(link).toBeVisible();
    await link.click();

    await expect(page).toHaveURL(new RegExp(`/instruments/${SYMBOL}$`));
    await expect(page.getByRole("heading", { level: 1, name: SYMBOL })).toBeVisible();
  });
});
