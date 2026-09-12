import { expect, test, type Page } from "@playwright/test";

import { dismissCookieBanner } from "./helpers/screen-chips";

import {
  openFilterGroup,
  openIndexSelect,
  openSortDirection,
} from "./helpers/screen-chips";

/**
 * Prompt 9's third acceptance criterion — URL round-trip via nuqs.
 * Updated for the chip-bar redesign.
 */
const EMAIL = "e2e@example.com";
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

test.describe("URL round-trip", () => {
  test("a full filter state survives a reload", async ({ page }) => {
    test.slow();
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });

    await openIndexSelect(page);
    await page.getByTestId("index-select").selectOption("nifty-500");
    await openSortDirection(page);
    await page.getByTestId("sort-direction").selectOption("asc");

    await openFilterGroup(page, "General Filters");
    await page.getByLabel("Apply filters on").selectOption("decile_3");
    await page.getByLabel("Minimum 1 year return (%)").fill("12");

    await openFilterGroup(page, "Away from High Filters");
    await page.getByLabel("Within % of all-time high").fill("30");

    await openFilterGroup(page, "Marketcap Range");
    await page.getByLabel("Marketcap range from (₹ cr)").fill("2500");

    await openFilterGroup(page, "Series");
    await expect(page.getByLabel("BE", { exact: true })).toBeChecked();
    await page.getByLabel("BE", { exact: true }).click();

    await openFilterGroup(page, "Ignore Top Beta / Volatility");
    await page.getByLabel("Ignore Top Beta Stocks").click();

    await expect(page).toHaveURL(/ignore_top_beta/);
    const shared = page.url();

    await page.reload();
    await expect(page).toHaveURL(shared);

    await openIndexSelect(page);
    await expect(page.getByTestId("index-select")).toHaveValue("nifty-500");
    await openSortDirection(page);
    await expect(page.getByTestId("sort-direction")).toHaveValue("asc");
    await openFilterGroup(page, "General Filters");
    await expect(page.getByLabel("Apply filters on")).toHaveValue("decile_3");
    await expect(page.getByLabel("Minimum 1 year return (%)")).toHaveValue("12");
    await openFilterGroup(page, "Away from High Filters");
    await expect(page.getByLabel("Within % of all-time high")).toHaveValue("30");
    await openFilterGroup(page, "Marketcap Range");
    await expect(page.getByLabel("Marketcap range from (₹ cr)")).toHaveValue("2500");
    await openFilterGroup(page, "Series");
    await expect(page.getByLabel("BE", { exact: true })).not.toBeChecked();
    await openFilterGroup(page, "Ignore Top Beta / Volatility");
    await expect(page.getByLabel("Ignore Top Beta Stocks")).toBeChecked();
  });

  test("a shared link reproduces the form in a fresh session", async ({ page, context }) => {
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await openIndexSelect(page);
    await page.getByTestId("index-select").selectOption("nifty-50");
    await expect(page).toHaveURL(/nifty-50/);
    const shared = page.url();

    const other = await context.newPage();
    await other.goto(shared);
    await openIndexSelect(other);
    await expect(other.getByTestId("index-select")).toHaveValue("nifty-50");
    await other.close();
  });

  test("the back button undoes one change at a time", async ({ page }) => {
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await openIndexSelect(page);
    await page.getByTestId("index-select").selectOption("nifty-50");
    await expect(page).toHaveURL(/nifty-50/);
    // Re-open if the popover closed after selection.
    await openIndexSelect(page);
    await page.getByTestId("index-select").selectOption("nifty-200");
    await expect(page).toHaveURL(/nifty-200/);

    await page.goBack();
    await openIndexSelect(page);
    await expect(page.getByTestId("index-select")).toHaveValue("nifty-50");
    await page.goBack();
    await openIndexSelect(page);
    await expect(page.getByTestId("index-select")).toHaveValue("nifty-total-market");
  });

  test("an unmodified screen has no query string to share", async ({ page }) => {
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });
    expect(new URL(page.url()).search).toBe("");
  });
});
