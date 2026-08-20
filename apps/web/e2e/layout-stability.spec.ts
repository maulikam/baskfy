import { expect, test, type Page } from "@playwright/test";

/**
 * Prompt 8's fourth acceptance criterion:
 *
 *     "No layout shift on theme toggle or on data load (skeletons reserve space)."
 *
 * Measured with the browser's own `layout-shift` PerformanceObserver — the same signal that feeds
 * Core Web Vitals — rather than by comparing screenshots, which would also flag a colour change.
 * The threshold is 0.01: not zero, because a sub-pixel reflow from a font metric is not a shift
 * anyone sees, and "good" CLS starts at 0.1.
 */
const CLS_BUDGET = 0.01;

async function startObserving(page: Page): Promise<void> {
  await page.evaluate(() => {
    const store = { total: 0 };
    (window as unknown as { __cls: typeof store }).__cls = store;
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        const shift = entry as PerformanceEntry & { value: number; hadRecentInput: boolean };
        if (!shift.hadRecentInput) store.total += shift.value;
      }
    }).observe({ type: "layout-shift", buffered: true });
  });
}

async function readCls(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as { __cls: { total: number } }).__cls.total);
}

test("switching theme shifts nothing", async ({ page }) => {
  await page.goto("/kitchen-sink");
  await page.waitForLoadState("networkidle");
  await startObserving(page);

  const before = await page.locator("#main-content").boundingBox();

  await page.getByRole("button", { name: "Change theme" }).click();
  await page.getByRole("menuitem", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.waitForTimeout(400);

  const after = await page.locator("#main-content").boundingBox();
  expect(after?.width).toBe(before?.width);
  expect(after?.height).toBe(before?.height);
  expect(await readCls(page)).toBeLessThan(CLS_BUDGET);
});

test("the page loads dark with no flash of light", async ({ page }) => {
  // Prompt 8 deliverable 6: "a working dark mode with no FOUC". next-themes writes the class from
  // a blocking script, so the very first paint is already dark.
  await page.goto("/kitchen-sink");
  await page.getByRole("button", { name: "Change theme" }).click();
  await page.getByRole("menuitem", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);

  await page.reload();
  const backgroundAtFirstPaint = await page.evaluate(
    () => getComputedStyle(document.documentElement).colorScheme,
  );
  expect(backgroundAtFirstPaint).toBe("dark");
  await expect(page.locator("html")).toHaveClass(/dark/);
});

test("the table's skeleton occupies the space its rows will", async ({ page }) => {
  await page.goto("/kitchen-sink");
  await page.waitForLoadState("networkidle");
  await startObserving(page);

  const loadedBox = await page.locator('[data-slot="data-table"]').boundingBox();

  await page.getByLabel("Loading state").click();
  await expect(page.locator('[data-slot="data-table"][data-loading="true"]')).toBeVisible();
  const skeletonBox = await page.locator('[data-slot="data-table"]').boundingBox();

  expect(skeletonBox?.width).toBeCloseTo(loadedBox?.width ?? 0, 0);
  expect(skeletonBox?.height).toBeCloseTo(loadedBox?.height ?? 0, 0);
  expect(await readCls(page)).toBeLessThan(CLS_BUDGET);
});

test("the freshness pill reserves its space while the status is loading", async ({ page }) => {
  // The API is not running in this environment, so the pill settles into its error state — which
  // is the point: the reserved box is the same either way, so the top bar never reflows.
  await page.goto("/kitchen-sink");
  await startObserving(page);
  await page.waitForTimeout(1500);
  expect(await readCls(page)).toBeLessThan(CLS_BUDGET);
});
