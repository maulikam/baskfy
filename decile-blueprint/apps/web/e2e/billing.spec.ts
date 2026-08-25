import { expect, test } from "@playwright/test";

import { SIGNED_OUT } from "./helpers/auth";

/**
 * `/pricing` end to end — PROMPTS.md Prompt 13 §5 and its fourth acceptance criterion.
 *
 *     "No price or entitlement is hard-coded in the web app; all read from the API."
 *
 * `src/lib/__tests__/no-hardcoded-pricing.test.ts` proves the *absence* of a literal by scanning
 * the source. This proves the other half: that the prices which appear on the page are the ones
 * the API served, by reading `GET /plans` in the same browser and comparing.
 *
 * The seeded catalogue is `baskfy_core.seed_data.PLANS` — docs/01 §1's three plans.
 */

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8100/api/v1";

interface PlanRow {
  code: string;
  label: string;
  price_inr: string;
  disclosure: string | null;
}

interface PlanPayload {
  data: PlanRow[];
  disclaimers: string[];
  free_tier_enabled: boolean;
}

/** `500.00` -> `₹500`, the way `Intl.NumberFormat("en-IN")` renders it. */
function rupees(amount: string): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(Number(amount));
}

test.describe("the pricing page", () => {
  test("shows exactly the plans and prices the API serves", async ({ page, request }) => {
    const payload = (await (await request.get(`${API}/plans`)).json()) as PlanPayload;
    expect(payload.data.length).toBeGreaterThan(0);

    await page.goto("/pricing");
    await expect(page.getByRole("heading", { name: "Pricing", level: 1 })).toBeVisible();

    for (const plan of payload.data) {
      const card = page.getByRole("article", { name: plan.label });
      await expect(card).toBeVisible();
      await expect(card.getByText(rupees(plan.price_inr), { exact: true })).toBeVisible();
    }

    // And nothing the API did not serve: a fourth card would mean a plan written into the app.
    await expect(page.getByRole("article")).toHaveCount(payload.data.length);
  });

  test("states, at the point of sale, what Forever means", async ({ page, request }) => {
    /** docs/11 §"Compliance & legal (India)", and PROMPTS.md Prompt 13 §5's own wording. */
    const payload = (await (await request.get(`${API}/plans`)).json()) as PlanPayload;
    const forever = payload.data.find((plan) => plan.code === "forever");
    expect(forever?.disclosure).toBeTruthy();

    await page.goto("/pricing");
    await expect(page.getByText("lifetime of the website", { exact: false })).toBeVisible();
  });

  test("carries the pre-purchase disclaimers and the SEBI disclaimer", async ({ page, request }) => {
    const payload = (await (await request.get(`${API}/plans`)).json()) as PlanPayload;

    await page.goto("/pricing");
    for (const line of payload.disclaimers) {
      await expect(page.getByText(line, { exact: false })).toBeVisible();
    }
    // docs/11 §Compliance lists three places the `<Disclaimer/>` must appear — "on every
    // analytics surface, in the footer, and **on checkout**". This is the checkout surface, so it
    // carries its own prominent copy *and* the app shell's footer copy: two, not one.
    const disclaimers = page.getByLabel("Regulatory disclaimer");
    await expect(disclaimers).toHaveCount(2);
    await expect(disclaimers.first()).toBeVisible();
    await expect(
      page.getByText("not a SEBI-registered investment adviser", { exact: false }).first(),
    ).toBeVisible();
  });

  test("asks a signed-out visitor to sign in rather than showing a button that fails", async ({
    page,
  }) => {
    /* Explicitly signed out: since the login gate closed, the suite signs in once in
       `auth.setup.ts` and every spec inherits that session unless it says otherwise. */
    await page.context().clearCookies();
    await page.goto("/pricing");
    const link = page.getByRole("link", { name: /sign in to choose/i }).first();
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("href", /\/login\?next=/);
  });

  test("never loads Razorpay's script on page load", async ({ page }) => {
    /**
     * `/pricing` is a public marketing page and is measured by Lighthouse. The widget's script is
     * injected on click and only on click — see `src/components/billing/razorpay.ts`.
     */
    const thirdParty: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("razorpay.com")) thirdParty.push(request.url());
    });
    await page.goto("/pricing");
    await page.waitForLoadState("networkidle");
    expect(thirdParty).toEqual([]);
  });
});

test.describe("the invoices page", () => {
  test.use({ storageState: SIGNED_OUT });

  test("sends a signed-out visitor to sign in", async ({ page }) => {
    await page.goto("/invoices");
    await expect(page).toHaveURL(/\/login/);
  });
});
