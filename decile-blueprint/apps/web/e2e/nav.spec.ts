import { expect, test, type Page } from "@playwright/test";

import { LEGACY_REDIRECTS, PRIMARY_NAV } from "../src/lib/nav";
import { PRIMARY_NAV_ID } from "../src/components/shell/ids";

/**
 * Navigation acceptance — Tree 6's baskfynavrefactorreport §4 and §6 step 7, plus SC9's `/home`.
 *
 * Five-item IA (Home first), reachable at tablet/desktop widths, mobile bottom tabs, legacy
 * 301/308 redirects, and screen results defaulting to basket view. Both nav walks iterate
 * `PRIMARY_NAV` rather than a literal list, so adding a destination cannot leave the e2e behind
 * — the count assertion below is what makes the addition deliberate.
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

test.describe("the primary navigation", () => {
  test("has exactly the five destinations the IA agreed on", () => {
    expect(PRIMARY_NAV.map((item) => item.label)).toEqual([
      "Home",
      "Market",
      "Baskets",
      "Build",
      "Me",
    ]);
  });

  for (const width of [768, 1024, 1280] as const) {
    test(`desktop nav shows Home · Market · Baskets · Build · Me at ${width}px`, async ({ page }) => {
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

  test("mobile bottom tab bar shows every reachable tab under 768px", async ({ page }) => {
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

test.describe("the home surface is the signed-in landing page", () => {
  test("carries net worth, pending actions, trending and collections", async ({ page }) => {
    test.slow();
    await page.setViewportSize({ width: 1280, height: 900 });
    await signIn(page);
    await page.goto("/home");

    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    await expect(page.getByLabel("Net worth")).toBeVisible();
    // Always present, even with nothing waiting — that is what the terminator card is for.
    await expect(page.getByTestId("pending-actions-terminator")).toBeVisible();
    await expect(page.getByTestId("trending-module")).toBeVisible();
    await expect(page.getByTestId("collections-grid")).toBeVisible();
  });

  test("is where the wordmark leads, and lights up its own nav pill", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await signIn(page);
    await page.goto("/market/today");

    await page.getByRole("link", { name: "Baskfy — home" }).click();
    await page.waitForURL(/\/home$/);

    const nav = page.locator(`#${PRIMARY_NAV_ID}`);
    await expect(nav.getByRole("link", { name: "Home", exact: true })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("is not the screener's market dashboard — /dashboard still goes to Market", async ({
    request,
  }) => {
    const response = await request.get("/dashboard", { maxRedirects: 0 });
    expect(response.headers().location ?? "").toContain("/market/today");
  });
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
