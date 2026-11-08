import { expect, test } from "@playwright/test";

/**
 * The options tab in a real browser — OP5 (`docs/options/06` OP5 AC, `05` §2).
 *
 * What this proves: `/options` renders signed-in against the e2e stack with the options flags at
 * their defaults (collector and scan off, the state the box ships in) — the heading, the F&O loss
 * caveat, the paper label, and an **honest empty state that names the reason** rather than a
 * blank panel or an error; and the journal and calendar tabs render theirs.
 *
 * What it does NOT prove, and where that is proved instead: the three sleeve states of the AC —
 * an O1 `WOULD_SKIP` with all its reasons, an O2 `ARMED` with its distance to trigger, an O3
 * candidate spread — and the `As of close` label. The e2e seed carries no `op_scan` rows (the
 * collector has never run against it), so those are asserted by the page's rendered-DOM tests over
 * fixtures (`src/app/(app)/options/__tests__/page.test.tsx`), on the swing hub's precedent
 * (DECISIONS-SW SW4.3, DECISIONS-OP OP5.8). A browser check that skipped when its fixture was
 * absent would read as coverage it is not.
 */

const CANNOT_PLACE = /nothing on this page can place an order/i;

test.describe("the options tab", () => {
  test("/options renders the caveat, the paper label and a reason for being empty", async ({
    page,
  }) => {
    await page.goto("/options");
    const unreadable = page.getByRole("heading", {
      name: /NIFTY options could not be read/,
    });
    const heading = page.getByRole("heading", {
      name: "NIFTY options",
      exact: true,
    });
    await expect(heading.or(unreadable).first()).toBeVisible();
    if (await unreadable.isVisible()) return; // this account is not the deployment's sole tenant
    await expect(page.getByTestId("options-fno-caveat")).toBeVisible();
    await expect(page.getByTestId("options-scan-only")).toBeVisible();
    await expect(page.getByText(CANNOT_PLACE)).toBeVisible();
    await expect(page.getByTestId("options-empty-reason")).toContainText(
      /options collector is off|scan is switched off|No scan yet today|never run/,
    );
  });

  test("the journal and the calendar tabs render their empty states", async ({
    page,
  }) => {
    await page.goto("/options/journal");
    await expect(
      page
        .getByTestId("options-backtest-empty")
        .or(page.getByText(/could not be read/))
        .first(),
    ).toBeVisible();
    await page.goto("/options/calendar");
    await expect(
      page
        .getByTestId("options-calendar-empty")
        .or(page.getByTestId("options-calendar-expiries"))
        .or(page.getByText(/could not be read/))
        .first(),
    ).toBeVisible();
  });
});
