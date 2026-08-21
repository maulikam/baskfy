import { expect, test, type Page } from "@playwright/test";

/**
 * Prompt 9's third acceptance criterion:
 *
 *     "A test asserts URL round-trip: encode a full filter state, reload, and the form matches."
 *
 * docs/08 §"Screen editor": "Entire form state is mirrored into the URL via `nuqs` → shareable,
 * back-button-correct." Three separate claims, and each is checked: the URL *carries* the state,
 * a *reload* restores it, and the *back button* undoes one change at a time.
 */
const EMAIL = "e2e@example.com";
/** `baskfy_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/screens/);
}

async function openGroup(page: Page, title: string): Promise<void> {
  const trigger = page.getByRole("button", { name: new RegExp(`^${title}`) });
  if ((await trigger.getAttribute("data-state")) !== "open") await trigger.click();
}

test.describe("URL round-trip", () => {
  test("a full filter state survives a reload", async ({ page }) => {
    test.slow();
    await signIn(page);
    await page.goto("/screens/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 results", { timeout: 20_000 });

    // Touch one field in each of several groups, across every shape the definition has:
    // an enum, a nested integer, a nested sentinel, a range, an array and a boolean switch.
    await page.getByTestId("index-select").selectOption("nifty-500");
    await page.getByTestId("sort-direction").selectOption("asc");

    await openGroup(page, "General Filters");
    await page.getByLabel("Apply filters on").selectOption("decile_3");
    await page.getByLabel("Minimum 1 year return (%)").fill("12");

    await openGroup(page, "Away from High Filters");
    await page.getByLabel("Within % of all-time high").fill("30");

    await openGroup(page, "Marketcap Range");
    await page.getByLabel("Marketcap range from (₹ cr)").fill("2500");

    // Investing 001 filters on both series (docs/13's export has two BE rows), so clicking BE
    // *removes* it — which is the interesting direction anyway: it proves an array shrinking
    // round-trips, not just one growing.
    await openGroup(page, "Series");
    await expect(page.getByLabel("BE", { exact: true })).toBeChecked();
    await page.getByLabel("BE", { exact: true }).click();

    await openGroup(page, "Ignore Top Beta / Volatility");
    await page.getByLabel("Ignore Top Beta Stocks").click();

    /*
     * The state is in the URL, not only in React — and *all* of it. The URL write is throttled to
     * the same 400 ms the preview debounces by (see `ScreenEditor`), so the assertion waits for
     * the last change to land rather than reloading into a half-written address.
     */
    await expect(page).toHaveURL(/ignore_top_beta/);
    const shared = page.url();

    await page.reload();
    await expect(page).toHaveURL(shared);

    await expect(page.getByTestId("index-select")).toHaveValue("nifty-500");
    await expect(page.getByTestId("sort-direction")).toHaveValue("asc");
    await openGroup(page, "General Filters");
    await expect(page.getByLabel("Apply filters on")).toHaveValue("decile_3");
    await expect(page.getByLabel("Minimum 1 year return (%)")).toHaveValue("12");
    await openGroup(page, "Away from High Filters");
    await expect(page.getByLabel("Within % of all-time high")).toHaveValue("30");
    await openGroup(page, "Marketcap Range");
    await expect(page.getByLabel("Marketcap range from (₹ cr)")).toHaveValue("2500");
    await openGroup(page, "Series");
    await expect(page.getByLabel("BE", { exact: true })).not.toBeChecked();
    await openGroup(page, "Ignore Top Beta / Volatility");
    await expect(page.getByLabel("Ignore Top Beta Stocks")).toBeChecked();
  });

  test("a shared link reproduces the form in a fresh session", async ({ page, context }) => {
    await signIn(page);
    await page.goto("/screens/exmpl0000001");
    await page.getByTestId("index-select").selectOption("nifty-50");
    await expect(page).toHaveURL(/nifty-50/);
    const shared = page.url();

    const other = await context.newPage();
    await other.goto(shared);
    await expect(other.getByTestId("index-select")).toHaveValue("nifty-50");
    await other.close();
  });

  test("the back button undoes one change at a time", async ({ page }) => {
    await signIn(page);
    await page.goto("/screens/exmpl0000001");
    // Each change waits for its own history entry: the URL write is throttled, so two selections
    // inside one window would coalesce into a single entry and Back would skip one.
    await page.getByTestId("index-select").selectOption("nifty-50");
    await expect(page).toHaveURL(/nifty-50/);
    await page.getByTestId("index-select").selectOption("nifty-200");
    await expect(page).toHaveURL(/nifty-200/);

    await page.goBack();
    await expect(page.getByTestId("index-select")).toHaveValue("nifty-50");
    await page.goBack();
    await expect(page.getByTestId("index-select")).toHaveValue("nifty-total-market");
  });

  test("an unmodified screen has no query string to share", async ({ page }) => {
    await signIn(page);
    await page.goto("/screens/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 results", { timeout: 20_000 });
    expect(new URL(page.url()).search).toBe("");
  });
});
