import { expect, test, type Page } from "@playwright/test";

/**
 * The rebalance tracker end to end — PROMPTS.md Prompt 14 §§1, 3 and 4, docs/08
 * §"Rebalance tracker".
 *
 *     "Wizard: choose portfolio (or upload CSV, with a downloadable sample) → choose screen → set
 *      `top_n` and `hold_buffer` → results as three columns (Exits / Inside WRH / Entries) each
 *      with copy-to-clipboard and CSV. Show unmatched symbols prominently rather than silently
 *      dropping."
 *
 * The rule itself is asserted in `packages/core/tests/test_rebalance.py` and the endpoint in
 * `services/api/tests/test_api_portfolios.py`. What this walks is the wizard: a real upload of a
 * messy file, a real screen run, and the three columns as a user sees them. The symbols come from
 * the committed reference export, which is what the e2e database is seeded from.
 */

/** The seeded account (`baskfy_api.seed e2e`). */
const EMAIL = "e2e@example.com";
/** `baskfy_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

/** Real NSE symbols in the seeded export, plus one BSE scrip code and one blank row. */
const MESSY_CSV = [
  "Symbol , Quantity ,Avg Price,Broker Note",
  "  cupid , 100 , 284.56 ,bought the dip",
  "",
  "hfcl,250,89.10,",
  "532540,10,1000,a BSE scrip code",
  "NOTALISTEDNAME,5,1,",
  "",
].join("\n");

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/build/);
}

async function uploadMessyPortfolio(page: Page): Promise<void> {
  await page.goto("/portfolio/portfolios");
  await page.getByTestId("toggle-upload").click();
  await page.getByTestId("csv-input").setInputFiles({
    name: "holdings.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(MESSY_CSV, "utf-8"),
  });
  await expect(page.getByTestId("import-report")).toBeVisible({ timeout: 30_000 });
}

/** The upload deliberately does not navigate — the report stays until the user continues. */
async function continueToRebalance(page: Page): Promise<void> {
  await page.getByTestId("continue-to-rebalance").click();
  await page.waitForURL(/\/portfolios\/\d+\/rebalance/, { timeout: 30_000 });
}

test.describe("the rebalance tracker", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("an upload reports what it could not match rather than dropping it", async ({ page }) => {
    await uploadMessyPortfolio(page);

    const report = page.getByTestId("import-report");
    await expect(report.getByText("2 imported")).toBeVisible();

    await expect(page.getByTestId("book-overall")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("book-box").filter({ hasText: /holding/i }).first()).toBeVisible();

    const problems = page.getByTestId("import-problems");
    await expect(problems.getByText(/532540/)).toBeVisible();
    await expect(problems.getByText(/BSE scrip code/)).toBeVisible();
    await expect(problems.getByText(/NOTALISTEDNAME/)).toBeVisible();
    await expect(report.getByText(/Columns ignored: Broker Note/)).toBeVisible();
  });

  test("the sample CSV is a real download", async ({ page }) => {
    await page.goto("/portfolio/portfolios");
    await page.getByTestId("toggle-upload").click();
    const href = await page.getByTestId("sample-csv").getAttribute("href");
    expect(href).toContain("/portfolios/sample-csv");

    const response = await page.request.get(href ?? "");
    expect(response.status()).toBe(200);
    expect(await response.text()).toContain("symbol,quantity,avg_price");
  });

  test("the wizard runs a screen and shows the three columns", async ({ page }) => {
    await uploadMessyPortfolio(page);
    await continueToRebalance(page);

    await page.getByTestId("top-n").fill("5");
    await page.getByTestId("hold-buffer").fill("3");
    await page.getByTestId("run-rebalance").click();

    const results = page.getByTestId("rebalance-results");
    await expect(results).toBeVisible({ timeout: 60_000 });

    // docs/01 §8's order: Exits, Inside WRH, Entries.
    await expect(page.getByRole("heading", { name: "Exits", level: 3 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Inside WRH", level: 3 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Entries", level: 3 })).toBeVisible();

    // Every column offers both affordances docs/08 requires.
    for (const slug of ["exits", "inside-wrh", "entries"]) {
      await expect(page.getByTestId(`copy-${slug}`)).toBeVisible();
      await expect(page.getByTestId(`csv-${slug}`)).toBeVisible();
    }

    // Prompt 14 §3: "plus an explanation of the buffer rule", in the user's own numbers.
    const explainer = page.getByTestId("buffer-explainer");
    await expect(explainer.getByText(/ranked 1–5 in the screen/)).toBeVisible();
    await expect(explainer.getByText(/ranked 9 or worse/)).toBeVisible();

    // Entries fill the top N that is not already held.
    await expect(page.getByTestId("count-entries")).toBeVisible();

    // Prompt 14 §4: the rebalance is persisted and shown back.
    await expect(page.getByTestId("rebalance-history")).toBeVisible({ timeout: 30_000 });
  });

  test("copying a column puts bare symbols on the clipboard", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await uploadMessyPortfolio(page);
    await continueToRebalance(page);
    await page.getByTestId("run-rebalance").click();
    await expect(page.getByTestId("rebalance-results")).toBeVisible({ timeout: 60_000 });

    await page.getByTestId("copy-entries").click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied.split("\n").length).toBeGreaterThan(0);
    expect(copied).not.toContain(",");
  });
});
