import { expect, test, type Page } from "@playwright/test";

/**
 * Prompt 9's first acceptance criterion, end to end:
 *
 *     "Playwright e2e: create a screen, set index + factor + three filters, apply, assert the
 *      result count and the first symbol against a seeded expectation, edit columns, export CSV,
 *      delete."
 *
 * The seeded expectation is derived from the committed reference export, not from a previous run
 * of this code. `fixtures/reference-screen-export-2026-08-18.csv` holds 271 NIFTY TOTAL MARKET
 * rows for 2026-08-18; of those, **87** have a marketcap of at least ₹50,000 crore and a median
 * 1-year volume of at least ₹1 crore, and the highest `mean(sharpe 1y, 6m, 3m, 1m)` among them is
 * **WELCORP** at 2.3025. Both numbers come from the file, so a regression in the screener changes
 * the test result rather than the expectation.
 */
const EXPECTED_COUNT = 87;
const EXPECTED_FIRST_SYMBOL = "WELCORP";

/** The seeded account (`baskfy_api.seed e2e`). Subscribed, because the export is gated. */
const EMAIL = "e2e@example.com";
/** `baskfy_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/screens/);
}

async function openGroup(page: Page, title: string): Promise<void> {
  const trigger = page.getByRole("button", { name: new RegExp(`^${title}`) });
  if ((await trigger.getAttribute("data-state")) !== "open") await trigger.click();
}

test.describe("the screens surface", () => {
  test("create, configure, apply, edit columns, export, delete", async ({ page }) => {
    test.slow();
    await signIn(page);

    // --- create ------------------------------------------------------------
    await page.goto("/screens");
    await expect(page.getByTestId("example-screens")).toContainText("Investing 001");
    await page.getByTestId("new-screen").click();
    await page.waitForURL(/\/screens\/[0-9a-f]{12}$/);
    const screenUrl = new URL(page.url());
    const publicId = screenUrl.pathname.split("/").at(-1) as string;

    // --- index + factor ----------------------------------------------------
    await page.getByTestId("index-select").selectOption("nifty-total-market");
    await page.getByRole("combobox", { name: "Sort By (Factor)" }).click();
    await page.getByPlaceholder(/Search 64 factors/).fill("AVERAGE SHARPE RETURN 12 6 3 1");
    await page.getByRole("option", { name: "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS" }).click();

    // --- three filters -----------------------------------------------------
    // 1. Series: add BE, so both series are in scope (docs/01 §2.9).
    await openGroup(page, "Series");
    await page.getByLabel("BE", { exact: true }).click();

    // 2. Median volume: ₹1 crore (docs/01 §2.2's preset list).
    await openGroup(page, "General Filters");
    await page.getByLabel("Median volume, 1 year").selectOption("10000000");

    // 3. Marketcap floor: ₹50,000 crore (docs/01 §2.7).
    await openGroup(page, "Marketcap Range");
    await page.getByLabel("Marketcap range from (₹ cr)").fill("50000");

    // --- the live preview settles on the seeded expectation ----------------
    await expect(page.getByTestId("result-count")).toHaveText(`${EXPECTED_COUNT} results`, {
      timeout: 20_000,
    });
    await expect(page.getByTestId("as-of")).toContainText("18 Aug 2026");
    await expect(page.getByTestId("sorting-factor")).toContainText(
      "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS",
    );
    const firstRow = page.locator('[role="row"][aria-rowindex="2"]');
    await expect(firstRow).toContainText(EXPECTED_FIRST_SYMBOL);

    // --- apply, which persists ---------------------------------------------
    await expect(page.getByTestId("unsaved-badge")).toBeVisible();
    await page.getByTestId("apply-filters").click();
    await expect(page.getByTestId("unsaved-badge")).toHaveCount(0);

    // A reload with no query string proves it was saved rather than only held in the URL.
    await page.goto(`/screens/${publicId}`);
    await expect(page.getByTestId("result-count")).toHaveText(`${EXPECTED_COUNT} results`, {
      timeout: 20_000,
    });

    // --- edit columns ------------------------------------------------------
    await page.getByRole("link", { name: "Edit Columns" }).click();
    await page.waitForURL(/\/columns$/);
    await page.getByLabel("RSI 1 YEAR").click();
    await expect(page.getByTestId("header-preview")).toContainText("RSI 1 YEAR");
    await page.getByTestId("save-columns").click();
    await page.waitForURL(/\/screens\/[0-9a-f]{12}$/);
    await expect(page.getByRole("columnheader", { name: "RSI 1 YEAR" })).toBeVisible({
      timeout: 20_000,
    });

    // --- export ------------------------------------------------------------
    const download = page.waitForEvent("download");
    await page.getByTestId("export-csv").click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);

    // --- delete ------------------------------------------------------------
    await page.goto("/screens");
    const card = page.locator(`[data-screen="${publicId}"]`);
    await expect(card).toBeVisible();
    await card.getByTestId("delete-screen").click();
    await page.getByTestId("confirm-delete").click();
    await expect(page.locator(`[data-screen="${publicId}"]`)).toHaveCount(0);
  });

  test("an example screen is read-only and previews without saving", async ({ page }) => {
    await signIn(page);
    await page.goto("/screens");
    await page.getByRole("link", { name: "Investing 001" }).click();
    await page.waitForURL(/\/screens\/exmpl0000001$/);

    // docs/13's own screen: 271 rows for 2026-08-18, CUPID first.
    await expect(page.getByTestId("result-count")).toHaveText("271 results", { timeout: 20_000 });
    await expect(page.locator('[role="row"][aria-rowindex="2"]')).toContainText("CUPID");

    await expect(page.getByTestId("apply-filters")).toBeDisabled();
  });

  test("clicking a row opens the peek drawer without leaving the screen", async ({ page }) => {
    await signIn(page);
    await page.goto("/screens/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 results", { timeout: 20_000 });

    await page.locator('[role="row"][aria-rowindex="2"]').click();
    const drawer = page.getByRole("dialog");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("CUPID");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).toHaveURL(/\/screens\/exmpl0000001/);
  });
});
