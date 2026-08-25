import { expect, test, type Page } from "@playwright/test";

/**
 * ⌘K reaches the whole catalog — `baskfynavrefactorreport` §F11, end to end.
 *
 * The unit tests mock the fetcher and the contract tests drive the API directly; neither can prove
 * the thing the report actually asked for, which is that a person pressing ⌘K in the running app
 * and typing a basket's name arrives at that basket. That claim spans the browser, the palette,
 * `GET /search`, the seeded database and the route table, so it is only provable here.
 *
 * The fixtures are the ones `baskfy_api.seed e2e` writes, and they are named rather than
 * discovered: `momentum-scan` / "Momentum Scan" (`baskfy_core.scan_projection`), the example
 * screens "Investing 001" and "Trend Stack" (`baskfy_core.seed_data.EXAMPLE_SCREENS`), the fourteen
 * seeded indices, and the 271-row reference export's instruments.
 */
const PLACEHOLDER = /Search stocks, indices, baskets, screens/;

async function openPalette(page: Page, query: string) {
  await page.goto("/market/today");
  await page.keyboard.press("ControlOrMeta+k");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await page.getByPlaceholder(PLACEHOLDER).fill(query);
  return dialog;
}

/** One group's items, by the heading the palette draws above them. */
function group(page: Page, heading: string) {
  return page.locator(`[cmdk-group]:has([cmdk-group-heading]:text-is("${heading}"))`);
}

test.describe("the ⌘K palette searches the whole catalog", () => {
  test("finds a basket and opens it", async ({ page }) => {
    await openPalette(page, "momentum");

    const baskets = group(page, "Baskets");
    await expect(baskets).toBeVisible();
    await baskets.getByText("Momentum Scan", { exact: false }).first().click();

    await expect(page).toHaveURL(/\/basket\/momentum-scan/);
  });

  test("finds a saved screen and opens it in Build", async ({ page }) => {
    await openPalette(page, "trend");

    const screens = group(page, "Screens");
    await expect(screens).toBeVisible();
    await screens.getByText("Trend Stack", { exact: false }).first().click();

    await expect(page).toHaveURL(/\/build\/exmpl\d+/);
  });

  test("finds an index and lands on it in the dashboard", async ({ page }) => {
    await openPalette(page, "midcap");

    const indices = group(page, "Indices");
    await expect(indices).toBeVisible();
    await indices.locator('[cmdk-item]').first().click();

    await expect(page).toHaveURL(/\/market\/today\?q=/);
    // The dashboard's own search box is URL state, so the row is actually filtered to.
    await expect(page.getByLabel("Search indices")).not.toHaveValue("");
  });

  test("still finds a stock, which is all it used to do", async ({ page }) => {
    await openPalette(page, "cupid");

    const stocks = group(page, "Stocks");
    await expect(stocks).toBeVisible();
    await stocks.locator('[cmdk-item]').first().click();

    await expect(page).toHaveURL(/\/instruments\//);
  });

  test("one query reaches three kinds at once", async ({ page }) => {
    /* The defect F11 named was fragmentation: three boxes, each seeing one slice. "momentum" is a
       word the seeded catalog carries across kinds — five NIFTY momentum indices, three example
       screens, and the Momentum Scan basket — so one box must now answer for all of them. */
    await openPalette(page, "momentum");

    for (const heading of ["Indices", "Screens", "Baskets"]) {
      await expect(
        page.locator(`[cmdk-group-heading]:text-is("${heading}")`),
        `no ${heading} group`,
      ).toBeVisible();
    }
  });

  test("remembers what was opened and offers it back on the next visit", async ({ page }) => {
    await openPalette(page, "momentum");
    await group(page, "Baskets").getByText("Momentum Scan", { exact: false }).first().click();
    await expect(page).toHaveURL(/\/basket\/momentum-scan/);

    await page.keyboard.press("ControlOrMeta+k");
    await expect(page.getByRole("dialog")).toBeVisible();
    // Empty box: the palette opens onto recents rather than onto nothing.
    await expect(group(page, "Recent")).toContainText("Momentum Scan");
  });
});
