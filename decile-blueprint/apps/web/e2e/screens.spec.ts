import { expect, test, type Page } from "@playwright/test";

import {
  closeFilterSurfaces,
  dismissCookieBanner,
  expectChipBarVisible,
  openFilterGroup,
  showResultsTable,
} from "./helpers/screen-chips";

/**
 * Prompt 9's first acceptance criterion, end to end — updated for the chip-bar redesign
 * (Phases 3–6). Canonical consumer route is `/build`.
 */
const EXPECTED_COUNT = 87;
const EXPECTED_FIRST_SYMBOL = "WELCORP";

const EMAIL = "e2e@example.com";
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/build/);
  await dismissCookieBanner(page);
}

test.describe("the screens surface", () => {
  test("create, configure, apply, edit columns, export, delete", async ({ page }) => {
    test.slow();
    await signIn(page);

    await page.goto("/build");
    await expect(page.getByTestId("example-screens")).toContainText("Investing 001");
    await page.getByTestId("new-screen").click();
    await page.waitForURL(/\/build\/[0-9a-f]{12}$/);
    const screenUrl = new URL(page.url());
    const publicId = screenUrl.pathname.split("/").at(-1) as string;

    await expectChipBarVisible(page);

    // --- index + factor via chips ------------------------------------------
    await page.getByTestId("chip-index").click();
    await page.getByTestId("index-select").selectOption("nifty-total-market");

    await page.getByTestId("chip-sort").click();
    await page.getByRole("combobox", { name: "Sort By (Factor)" }).click();
    await page.getByPlaceholder(/Search 64 factors/).fill("AVERAGE SHARPE RETURN 12 6 3 1");
    await page.getByRole("option", { name: /AVERAGE SHARPE RETURN 12 6 3 1 MONTHS/i }).click();

    // --- three filters via + Filter ----------------------------------------
    await openFilterGroup(page, "Series");
    await page.getByLabel("BE", { exact: true }).click();

    await openFilterGroup(page, "General Filters");
    await page.getByLabel("Median volume, 1 year").selectOption("10000000");

    await openFilterGroup(page, "Marketcap Range");
    await page.getByLabel("Marketcap range from (₹ cr)").fill("50000");

    await expect(page.getByTestId("result-count")).toHaveText(`${EXPECTED_COUNT} matches`, {
      timeout: 20_000,
    });
    await expect(page.getByTestId("as-of")).toContainText("18 Aug 2026");
    await expect(page.getByTestId("sorting-factor")).toContainText(
      "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS",
    );
    await expect(page.getByTestId("story-strip")).toBeVisible();
    await showResultsTable(page);
    const firstRow = page.locator('[role="row"][aria-rowindex="2"]');
    await expect(firstRow).toContainText(EXPECTED_FIRST_SYMBOL);

    await closeFilterSurfaces(page);
    await expect(page.getByTestId("unsaved-badge")).toBeVisible();
    await page.getByTestId("apply-filters").click();
    await expect(page.getByTestId("unsaved-badge")).toHaveCount(0);

    await page.goto(`/build/${publicId}`);
    await expect(page.getByTestId("result-count")).toHaveText(`${EXPECTED_COUNT} matches`, {
      timeout: 20_000,
    });

    await page.getByRole("link", { name: "Edit columns" }).click();
    await page.waitForURL(/\/columns$/);
    await page.getByLabel("RSI 1 YEAR").click();
    await expect(page.getByTestId("header-preview")).toContainText("RSI 1 YEAR");
    await page.getByTestId("save-columns").click();
    await page.waitForURL(/\/build\/[0-9a-f]{12}$/);
    await expect(page.getByTestId("result-count")).toHaveText(`${EXPECTED_COUNT} matches`, {
      timeout: 20_000,
    });
    await showResultsTable(page);
    const rsiHeader = page.getByRole("columnheader", { name: /RSI 1 YEAR/i });
    await rsiHeader.scrollIntoViewIfNeeded();
    await expect(rsiHeader).toBeVisible({ timeout: 20_000 });

    // Export CSV is one level down in the Export menu (§3.3).
    const download = page.waitForEvent("download");
    await page.getByTestId("export-menu").click();
    await page.getByTestId("export-csv").click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);

    // Share card opens.
    await page.getByTestId("share-screen").click();
    await expect(page.getByRole("dialog")).toContainText("Share this screen");
    await page.keyboard.press("Escape");

    await page.goto("/build");
    const card = page.locator(`[data-screen="${publicId}"]`);
    await expect(card).toBeVisible();
    await card.getByTestId("delete-screen").click();
    await page.getByTestId("confirm-delete").click();
    await expect(page.locator(`[data-screen="${publicId}"]`)).toHaveCount(0);
  });

  test("an example screen is read-only and previews without saving", async ({ page }) => {
    await signIn(page);
    await page.goto("/build");
    await page.getByRole("link", { name: "Investing 001" }).click();
    await page.waitForURL(/\/build\/exmpl0000001$/);

    await expectChipBarVisible(page);
    // Demo banner is dismissible; Duplicate is the durable read-only cue. `.first()` so both
    // being present (fresh load) is not a strict-mode failure.
    await expect(
      page.getByTestId("demo-banner").or(page.getByRole("button", { name: /Duplicate/i })).first(),
    ).toBeVisible();

    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });
    await showResultsTable(page);
    await expect(page.locator('[role="row"][aria-rowindex="2"]')).toContainText("CUPID");
    await expect(page.getByTestId("story-strip")).toBeVisible();

    await expect(page.getByTestId("apply-filters")).toBeDisabled();
  });

  test("clicking a row opens the peek drawer without leaving the screen", async ({ page }) => {
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });
    await showResultsTable(page);

    await page.locator('[role="row"][aria-rowindex="2"]').click();
    const drawer = page.getByRole("dialog");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("CUPID");
    await expect(drawer.getByRole("link", { name: /Open the full factsheet/i })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).toHaveURL(/\/build\/exmpl0000001/);
  });
});
