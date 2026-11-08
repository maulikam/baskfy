import { expect, test, type Page } from "@playwright/test";

import { dismissCookieBanner } from "./helpers/screen-chips";

import {
  chipActiveCount,
  openFilterGroup,
} from "./helpers/screen-chips";

/**
 * Prompt 9's second acceptance criterion — sentinel fields + active counts.
 * Updated for the chip-bar redesign: badges live on filled chips, not accordion headers.
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

test.describe("sentinel values", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await page.goto("/build/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });
  });

  test("away from high: 100 means ignore", async ({ page }) => {
    const title = "Away from High Filters";
    await openFilterGroup(page, title);
    expect(await chipActiveCount(page, title)).toBe(0);

    const field = page.getByLabel("Within % of all-time high");
    await field.fill("25");
    const hint = page.locator(`#${(await field.getAttribute("aria-describedby")) as string}`);
    await expect(hint).not.toContainText("This filter is currently off.");
    expect(await chipActiveCount(page, title)).toBe(1);

    await field.fill("100");
    await expect(hint).toContainText("This filter is currently off.");
    expect(await chipActiveCount(page, title)).toBe(0);
  });

  test("positive days: 0 means ignore", async ({ page }) => {
    const title = "Percentage of Positive Days Filters";
    await openFilterGroup(page, title);
    expect(await chipActiveCount(page, title)).toBe(0);

    const field = page.getByLabel("Minimum positive days, 1 Year");
    await field.fill("55");
    expect(await chipActiveCount(page, title)).toBe(1);

    await field.fill("0");
    const describedBy = (await field.getAttribute("aria-describedby")) as string;
    await expect(page.locator(`#${describedBy}`)).toContainText("This filter is currently off.");
    expect(await chipActiveCount(page, title)).toBe(0);
  });

  test("circuits: the sentinel is a threshold, not a value", async ({ page }) => {
    const title = "Circuit Filters";
    await openFilterGroup(page, title);
    expect(await chipActiveCount(page, title)).toBe(0);

    const field = page.getByLabel("Maximum circuit days, 1 Year");

    await field.fill("250");
    expect(await chipActiveCount(page, title)).toBe(1);

    await field.fill("251");
    const describedBy = (await field.getAttribute("aria-describedby")) as string;
    await expect(page.locator(`#${describedBy}`)).toContainText("This filter is currently off.");
    expect(await chipActiveCount(page, title)).toBe(0);
  });

  test("an off field is visibly marked, not just silently inert", async ({ page }) => {
    await openFilterGroup(page, "Away from High Filters");

    const marks = page.getByText("Off", { exact: true });
    await expect(marks).toHaveCount(2);

    await page.getByLabel("Within % of all-time high").fill("25");
    await expect(marks).toHaveCount(1);

    await page.getByLabel("Within % of 1 year high").fill("10");
    await expect(marks).toHaveCount(0);
  });

  test("a group badge counts only what is doing something", async ({ page }) => {
    const title = "Percentage of Positive Days Filters";
    await openFilterGroup(page, title);
    await page.getByLabel("Minimum positive days, 1 Year").fill("55");
    await page.getByLabel("Minimum positive days, 3 Months").fill("60");
    expect(await chipActiveCount(page, title)).toBe(2);

    await page.getByLabel("Minimum positive days, 3 Months").fill("0");
    expect(await chipActiveCount(page, title)).toBe(1);
  });
});
