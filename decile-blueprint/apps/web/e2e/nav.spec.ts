import { expect, test, type Page } from "@playwright/test";

import { LEGACY_REDIRECTS, PRIMARY_NAV } from "../src/lib/nav";
import { PRIMARY_NAV_ID } from "../src/components/shell/ids";

/**
 * Tree 6 navigation acceptance — baskfynavrefactorreport §4 and §6 step 7.
 *
 * Four-item IA, reachable at tablet/desktop widths, mobile bottom tabs, legacy 301/308 redirects,
 * and screen results defaulting to basket view.
 */

const EMAIL = "e2e@example.com";
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/build/);
}

test.describe("the four-item primary navigation", () => {
  for (const width of [768, 1024, 1280] as const) {
    test(`desktop nav shows Market · Baskets · Build · Me at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/market/today");

      const nav = page.locator(`#${PRIMARY_NAV_ID}`);
      await expect(nav).toBeVisible();

      for (const item of PRIMARY_NAV) {
        const link = nav.getByRole("link", { name: item.label, exact: true });
        await expect(link, `${item.label} must be visible at ${width}px`).toBeVisible();
        await expect(link).toBeInViewport();
      }
    });
  }

  test("mobile bottom tab bar shows four reachable tabs under 768px", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/market/today");

    await expect(page.locator(`#${PRIMARY_NAV_ID}`)).toBeHidden();

    const tabs = page.getByTestId("bottom-tab-bar");
    await expect(tabs).toBeVisible();

    for (const item of PRIMARY_NAV) {
      const link = tabs.getByRole("link", { name: item.label, exact: true });
      await expect(link).toBeVisible();
      await expect(link).toBeInViewport();
    }
  });

  test("aria-current marks the active primary destination", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/build");

    const nav = page.locator(`#${PRIMARY_NAV_ID}`);
    await expect(nav.getByRole("link", { name: "Build", exact: true })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(nav.getByRole("link", { name: "Market", exact: true })).not.toHaveAttribute(
      "aria-current",
    );
  });
});

test.describe("legacy consumer routes permanently redirect", () => {
  for (const { source, destination } of LEGACY_REDIRECTS) {
    test(`${source} → ${destination}`, async ({ request }) => {
      const response = await request.get(source, { maxRedirects: 0 });
      expect(response.status(), `${source} must redirect`).toBeGreaterThanOrEqual(301);
      expect(response.status()).toBeLessThanOrEqual(308);
      const location = response.headers().location ?? "";
      expect(location.replace(/\/$/, "")).toContain(destination.replace(/\/$/, ""));
    });
  }
});

test.describe("screen results materialize as basket view", () => {
  test("an example template opens with basket view selected by default", async ({ page }) => {
    test.slow();
    await page.setViewportSize({ width: 1280, height: 900 });
    await signIn(page);
    await page.goto("/build/exmpl0000001");

    await expect(page.getByTestId("result-count")).toHaveText("271 matches", { timeout: 20_000 });
    await expect(page.getByTestId("view-mode-basket")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("heading", { level: 1, name: "Investing 001" })).toBeVisible();
    await expect(page.getByText("Kept as cash")).toBeVisible();
  });
});
