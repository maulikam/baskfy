import { expect, test, type Page } from "@playwright/test";

/**
 * Prompt 9's second acceptance criterion:
 *
 *     "Playwright e2e: toggling a sentinel field to its 'ignore' value visibly marks it inactive
 *      and removes it from the group's active count."
 *
 * docs/08 §"Screen editor" is where both halves come from — "the field renders visually 'off' when
 * at its sentinel", and "Each accordion header shows a **count badge** of active filters inside
 * it, so a collapsed group never hides state".
 *
 * The three sentinel *shapes* are all exercised, because they are not the same rule: away-from-high
 * is `= 100`, positive days is `= 0`, and circuits is a **threshold** (`> 250`), so 250 is a live
 * cap and 999 is not (docs/01 §2.4–§2.6). A UI that understood only equality would leave the
 * circuit filter silently applied.
 */
const EMAIL = "e2e@example.com";
/** `decile_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/screens/);
}

function group(page: Page, title: string) {
  return page.getByRole("button", { name: new RegExp(`^${title}`) });
}

async function openGroup(page: Page, title: string): Promise<void> {
  const trigger = group(page, title);
  if ((await trigger.getAttribute("data-state")) !== "open") await trigger.click();
}

/** The badge is inside the accordion trigger; absent means no active filters in that group. */
async function activeCount(page: Page, title: string): Promise<number> {
  const badge = group(page, title).locator("span.rounded-full");
  if ((await badge.count()) === 0) return 0;
  const text = (await badge.first().innerText()).trim();
  return Number.parseInt(text, 10) || 0;
}

test.describe("sentinel values", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await page.goto("/screens/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 results", { timeout: 20_000 });
  });

  test("away from high: 100 means ignore", async ({ page }) => {
    const title = "Away from High Filters";
    await openGroup(page, title);
    expect(await activeCount(page, title)).toBe(0);

    const field = page.getByLabel("Within % of all-time high");
    await field.fill("25");
    // Scoped to *this* field's own description: the group's other sentinel (the 1-year high) is
    // still at 100, so an unscoped search would find its "off" text and prove nothing.
    const hint = page.locator(`#${(await field.getAttribute("aria-describedby")) as string}`);
    await expect(hint).not.toContainText("This filter is currently off.");
    expect(await activeCount(page, title)).toBe(1);

    await field.fill("100");
    // The field says so, and the badge stops counting it.
    await expect(hint).toContainText("This filter is currently off.");
    expect(await activeCount(page, title)).toBe(0);
  });

  test("positive days: 0 means ignore", async ({ page }) => {
    const title = "Percentage of Positive Days Filters";
    await openGroup(page, title);
    expect(await activeCount(page, title)).toBe(0);

    const field = page.getByLabel("Minimum positive days, 1 Year");
    await field.fill("55");
    expect(await activeCount(page, title)).toBe(1);

    await field.fill("0");
    const describedBy = (await field.getAttribute("aria-describedby")) as string;
    await expect(page.locator(`#${describedBy}`)).toContainText("This filter is currently off.");
    expect(await activeCount(page, title)).toBe(0);
  });

  test("circuits: the sentinel is a threshold, not a value", async ({ page }) => {
    const title = "Circuit Filters";
    await openGroup(page, title);
    expect(await activeCount(page, title)).toBe(0);

    const field = page.getByLabel("Maximum circuit days, 1 Year");

    // docs/01 §2.6: "> 250 = ignore", so 250 itself is a live cap.
    await field.fill("250");
    expect(await activeCount(page, title)).toBe(1);

    await field.fill("251");
    const describedBy = (await field.getAttribute("aria-describedby")) as string;
    await expect(page.locator(`#${describedBy}`)).toContainText("This filter is currently off.");
    expect(await activeCount(page, title)).toBe(0);
  });

  test("an off field is visibly marked, not just silently inert", async ({ page }) => {
    // "General Filters" opens by default and has an off field of its own, so it is collapsed
    // first — otherwise the count below is measuring two groups at once.
    await group(page, "General Filters").click();
    await openGroup(page, "Away from High Filters");

    // Two sentinel fields in this group; both start at 100, so both start marked off.
    const marks = page.getByText("Off", { exact: true });
    await expect(marks).toHaveCount(2);

    await page.getByLabel("Within % of all-time high").fill("25");
    await expect(marks).toHaveCount(1);

    await page.getByLabel("Within % of 1 year high").fill("10");
    await expect(marks).toHaveCount(0);
  });

  test("a group badge counts only what is doing something", async ({ page }) => {
    const title = "Percentage of Positive Days Filters";
    await openGroup(page, title);
    await page.getByLabel("Minimum positive days, 1 Year").fill("55");
    await page.getByLabel("Minimum positive days, 3 Months").fill("60");
    expect(await activeCount(page, title)).toBe(2);

    await page.getByLabel("Minimum positive days, 3 Months").fill("0");
    expect(await activeCount(page, title)).toBe(1);
  });
});
