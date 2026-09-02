import { expect, test } from "@playwright/test";

/**
 * The swing hub in a real browser — SW4's deferred check, run by SW11 (docs/swing/06 SW11;
 * DECISIONS-SW SW4.3).
 *
 * What this proves: `/swing` (the setups page) renders signed-in against the e2e database — the
 * heading, the section tabs, the empty state written from the funnel (or a table when the
 * database carries a detection row), and the footnote that nothing on the page can place an
 * order — and the same for the market and journal tabs.
 *
 * What it does NOT prove, still: "one flag and one locked EP with the lock icon" (SW4's exact
 * wording). The e2e seed carries no detection rows — the detectors need 125 sessions of bars
 * the fixture market does not have (SW0.2, SW3.3) — so the lock icon is covered by the page's
 * rendered-DOM tests over a mocked fetch (`src/app/(app)/swing/__tests__`), not here. A
 * browser check that skipped when its fixture was absent would read as coverage it is not.
 */

const CANNOT_PLACE = /nothing on this page can place an order/i;

test.describe("the swing hub", () => {
  test("/swing renders the setups page with its empty state and the cannot-place footnote", async ({
    page,
  }) => {
    await page.goto("/swing");
    await expect(page.getByRole("heading", { name: "Swing", exact: true })).toBeVisible();
    await expect(page.getByText(CANNOT_PLACE)).toBeVisible();
    // Either a detection table or the funnel-written empty state — never a blank panel.
    const table = page.getByRole("table");
    const empty = page.getByText(/^No flags (today )?—/i);
    await expect(table.or(empty).first()).toBeVisible();
  });

  test("the section tabs reach the market and the journal, and the journal cannot place", async ({
    page,
  }) => {
    await page.goto("/swing");
    await page.getByRole("link", { name: "Market", exact: true }).first().click();
    await expect(page).toHaveURL(/\/swing\/market/);
    await expect(page.getByText(/The tape is|No market row has been written yet/i).first()).toBeVisible();
    await page.goto("/swing/journal");
    await expect(page.getByText(CANNOT_PLACE)).toBeVisible();
  });
});
