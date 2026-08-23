import { expect, test, type Page } from "@playwright/test";

/**
 * Tree-2 leaf 2.5 — explore → Invest → PlanHandoff smoke.
 *
 * SC5 / docs/smallcase: Invest CTAs open `PlanHandoffPanel` (or the market-closed modal).
 * Web never places orders; execution stays on the desk. This walk asserts the hand-off copy
 * and deliberately never clicks an execute control.
 *
 * Honest skips: without a Playwright `baseURL` (webServer / env) or when `/explore` needs
 * a session the suite cannot establish, the test skips rather than flake.
 */

/** The seeded account (`baskfy_api.seed e2e`). */
const EMAIL = "e2e@example.com";
/** `baskfy_api.seed.E2E_PASSWORD` — a published constant for a throwaway database. */
const E2E_PASSWORD = "e2e-suite-password";

async function signIn(page: Page): Promise<boolean> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  try {
    await page.waitForURL(/\/screens/, { timeout: 20_000 });
    return true;
  } catch {
    return false;
  }
}

test.describe("explore → invest hand-off", () => {
  test("Invest CTA opens PlanHandoff and never clicks execute", async ({ page, baseURL }) => {
    test.skip(
      !baseURL,
      "no baseURL — Playwright webServer / baseURL not configured; skip rather than flake",
    );

    await page.goto("/explore");

    // Middleware may not gate /explore, but a deployment can still bounce anonymous visitors.
    if (/\/login/.test(page.url())) {
      const signedIn = await signIn(page);
      test.skip(!signedIn, "auth required and e2e sign-in failed — skip rather than flake");
      await page.goto("/explore");
      test.skip(/\/login/.test(page.url()), "still on /login after sign-in — auth env incomplete");
    }

    // Empty catalog (seed without SCAN basket) is an env gap, not a product failure here.
    const cards = page.getByRole("list", { name: "Basket catalog" }).getByRole("link");
    if ((await cards.count()) === 0) {
      test.skip(true, "no published baskets on /explore — seed managers + SCAN basket first");
    }

    await cards.first().click();
    await expect(page).toHaveURL(/\/basket\//);

    // Invest opens the hand-off panel. Never touch Execute / Confirm / place-order controls.
    await page.getByRole("button", { name: "Invest now" }).click();

    const handoff = page.getByRole("complementary", { name: "Plan hand-off" });
    await expect(handoff).toBeVisible();
    // Stub (no plan_id yet): "desk". With a real plan: "Plan #" and/or "expires".
    await expect(handoff).toContainText(/Plan #|desk|expires/i);

    await expect(page.getByRole("button", { name: /^(Execute|Confirm|Place order)$/i })).toHaveCount(
      0,
    );
    await expect(handoff.getByRole("button", { name: /execute/i })).toHaveCount(0);
  });
});
