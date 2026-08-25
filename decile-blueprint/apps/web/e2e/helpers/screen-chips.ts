import { expect, type Page } from "@playwright/test";

/** Filter group titles → chip ids (matches `FILTER_GROUPS` in lib/screens/groups.ts). */
export const FILTER_GROUP_IDS: Readonly<Record<string, string>> = {
  "General Filters": "general",
  "Moving Average Filters": "moving-average",
  "Away from High Filters": "away-from-high",
  "Percentage of Positive Days Filters": "positive-days",
  "Circuit Filters": "circuits",
  "Marketcap Range": "marketcap",
  "Price to Earnings Range": "pe",
  Series: "series",
  "Ignore Top Beta / Volatility": "risk",
  "Price (CMP) Range": "price",
  "Multi-Factor Combined Ranking": "multi-factor",
  "Historical Ranks": "historical",
  "Custom Filters": "custom",
};

/** Open a filter group via filled chip or "+ Filter" search (chip-bar redesign). */

export async function closeFilterSurfaces(page: Page): Promise<void> {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const dialog = page.getByTestId("draft-filter-dialog");
    if (await dialog.isVisible().catch(() => false)) {
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0, { timeout: 3_000 }).catch(() => undefined);
      continue;
    }
    const modal = page.getByRole("dialog");
    if (await modal.isVisible().catch(() => false)) {
      await page.keyboard.press("Escape");
      continue;
    }
    break;
  }
}

export async function openFilterGroup(page: Page, title: string): Promise<void> {
  await closeFilterSurfaces(page);
  const groupId = FILTER_GROUP_IDS[title];
  if (!groupId) throw new Error(`Unknown filter group: ${title}`);

  const chip = page.getByTestId(`chip-filter-${groupId}`);
  if (await chip.isVisible().catch(() => false)) {
    await chip.click();
    return;
  }

  await page.getByTestId("chip-add-filter").click();
  await page.getByTestId("filter-search").fill(title);
  await page.getByRole("option", { name: new RegExp(title, "i") }).first().click();

  const dialog = page.getByTestId("draft-filter-dialog");
  if (await dialog.isVisible().catch(() => false)) return;

  await expect(chip).toBeVisible();
  await chip.click();
}

/** Active filter count from the filled chip (0 if the chip is absent). */
export async function chipActiveCount(page: Page, title: string): Promise<number> {
  const groupId = FILTER_GROUP_IDS[title];
  if (!groupId) throw new Error(`Unknown filter group: ${title}`);
  const chip = page.getByTestId(`chip-filter-${groupId}`);
  if (!(await chip.isVisible().catch(() => false))) return 0;
  const text = (await chip.innerText()).replace(/\s+/g, " ");
  const match = /·\s*(\d+)/.exec(text);
  if (match?.[1]) return Number.parseInt(match[1], 10);
  return 1;
}



/** Cookie banner and the apply pill both sit at the bottom; dismiss so e2e can click Apply. */
export async function dismissCookieBanner(page: Page): Promise<void> {
  const banner = page.getByRole("region", { name: "Cookie choices" });
  if (await banner.isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "Essential only" }).click();
    await expect(banner).toHaveCount(0);
  }
}

/** Basket view is the default; row-level table tests need the Table toggle (Tree 6 §5). */
export async function showResultsTable(page: Page): Promise<void> {
  await closeFilterSurfaces(page);
  const toggle = page.getByTestId("view-mode-table");
  if (!(await toggle.isVisible().catch(() => false))) return;
  if ((await toggle.getAttribute("aria-pressed")) === "true") return;
  await toggle.click();
  await expect(page.locator('[role="row"][aria-rowindex="2"]')).toBeVisible({ timeout: 20_000 });
}

export async function openIndexSelect(page: Page): Promise<void> {
  if (await page.getByTestId("index-select").isVisible().catch(() => false)) return;
  await page.getByTestId("chip-index").click();
  await expect(page.getByTestId("index-select")).toBeVisible();
}

/**
 * Always-visible chip-bar chrome (Index · Sorted by · Direction · Liquidity · + Filter).
 * Visibility only — opening Liquidity would steal focus from the other chip journeys.
 */
export async function expectChipBarVisible(page: Page): Promise<void> {
  await expect(page.getByTestId("filter-chip-bar")).toBeVisible();
  await expect(page.getByTestId("chip-liquidity")).toBeVisible();
}

export async function openSortDirection(page: Page): Promise<void> {
  if (await page.getByTestId("sort-direction").isVisible().catch(() => false)) return;
  await page.getByTestId("chip-direction").click();
  await expect(page.getByTestId("sort-direction")).toBeVisible();
}
