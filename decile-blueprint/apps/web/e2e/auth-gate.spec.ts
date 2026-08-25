import { expect, test, type Page } from "@playwright/test";

import { DEFAULT_DESTINATION, E2E_EMAIL, E2E_PASSWORD, SIGNED_OUT } from "./helpers/auth";

/**
 * The login gate, from the browser's side.
 *
 * Two things are asserted here that no unit test can reach, because both are about the *browser's*
 * memory rather than the server's answer:
 *
 * - **A direct URL hit.** Typing `/build` into the address bar, or following a bookmark, with no
 *   session. The middleware answers, and the `?next=` round trip has to actually return the person
 *   to where they were going once they sign in.
 * - **The Back button after signing out.** This is the one that looks fine in every unit test and
 *   fails in real life: the browser re-paints the previous page from its back/forward cache
 *   without asking the server anything. `Cache-Control: no-store` on every gated response is what
 *   disqualifies the page from that cache, and this spec is the only place that gets checked.
 */

/**
 * A sample across the route groups, not an exhaustive list — `public-routes.test.ts` covers the
 * predicate for every path. These are canonical paths on purpose: `/dashboard`, `/listings`,
 * `/portfolios` and friends are 308s in `next.config.ts` (`LEGACY_REDIRECTS`), which run *before*
 * the middleware, so a bookmark of one arrives at the gate as its destination. That is asserted
 * separately below, because "the old URL still works, and it still asks you to sign in" is the
 * thing a person with a year-old bookmark actually experiences.
 */
const GATED = [
  "/build",
  "/baskets",
  "/holdings",
  "/me/portfolios",
  "/me/watchlist",
  "/market/today",
  "/market/mood",
  "/market/listings",
  "/instruments/CUPID",
  "/profile",
  "/invoices",
] as const;

/** Old URL → where `next.config.ts` sends it, so the gate quotes the destination in `?next=`. */
const LEGACY = [
  ["/dashboard", "/market/today"],
  ["/market-health", "/market/mood"],
  ["/listings", "/market/listings"],
  ["/explore", "/baskets"],
  ["/portfolios", "/me/portfolios"],
] as const;

const PUBLIC = ["/", "/pricing", "/faq", "/about", "/privacy-policy", "/terms-conditions"] as const;

/**
 * The page's path and query, without the origin.
 *
 * `toHaveURL` against a full string cannot be used here: `next start --hostname 127.0.0.1` answers
 * a middleware redirect with `localhost` in the `Location`, so the browser ends up on a different
 * *host* spelling than `baseURL`. Irrelevant to the behaviour under test and true before this
 * change too — but it makes a whole-URL assertion a test of Next's host canonicalisation.
 */
function where(page: { url(): string }): string {
  const url = new URL(page.url());
  return `${url.pathname}${url.search}`;
}

/** Fill and submit the password tab of whatever login page is currently open. */
async function signInHere(page: Page): Promise<void> {
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(E2E_EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test.describe("signed out", () => {
  test.use({ storageState: SIGNED_OUT });

  for (const path of GATED) {
    test(`a direct hit on ${path} lands on the login page`, async ({ page }) => {
      await page.goto(path);
      expect(where(page)).toBe(`/login?next=${encodeURIComponent(path)}`);
      await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    });
  }

  for (const [legacy, destination] of LEGACY) {
    test(`an old bookmark of ${legacy} is redirected and then gated`, async ({ page }) => {
      await page.goto(legacy);
      expect(where(page)).toBe(`/login?next=${encodeURIComponent(destination)}`);
    });
  }

  for (const path of PUBLIC) {
    test(`${path} is still readable without an account`, async ({ page }) => {
      await page.goto(path);
      expect(where(page)).toBe(path);
    });
  }

  test("the gate returns you to the page you asked for, query and all", async ({ page }) => {
    await page.goto("/market/listings?search=CUPID");
    expect(where(page)).toBe(`/login?next=${encodeURIComponent("/market/listings?search=CUPID")}`);

    await signInHere(page);
    await page.waitForURL(/\/market\/listings\?search=CUPID/, { timeout: 30_000 });
    expect(where(page)).toBe("/market/listings?search=CUPID");
  });

  test("`next` cannot be pointed off-site", async ({ page }) => {
    await page.goto("/login?next=https://example.com/phishing");
    await signInHere(page);

    /* Wait for the sign-in to actually land somewhere before reading the URL — the assertion is
       about where it went, and `/login` is still `/login` for the moment before it goes. */
    await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 30_000 });
    expect(page.url()).not.toContain("example.com");
    /* `src/app/actions/auth.ts` falls back to its default destination rather than honouring a
       non-relative `next`. An open redirect on a sign-in page is a phishing primitive. */
    expect(where(page)).toBe(DEFAULT_DESTINATION);
  });

  test("a gated route refuses a prefetch as well as a navigation", async ({ request }) => {
    /* The hole this covers: `<Link>` prefetches used to skip the middleware entirely, so the
       signed-in payload of a gated page was fetched and cached for a visitor who had never signed
       in. Playwright's `request` fixture carries the spec's (empty) storage state. */
    const response = await request.get("/build", {
      headers: { RSC: "1", "Next-Router-Prefetch": "1" },
      maxRedirects: 0,
    });
    expect(response.status()).toBe(307);
    expect(response.headers()["location"]).toContain("/login");
  });
});

test.describe("signed in", () => {
  test("a gated page tells the browser not to store it", async ({ page }) => {
    const response = await page.goto(DEFAULT_DESTINATION);
    expect(response?.status()).toBe(200);
    /* No stored copy and no bfcache entry — which is what makes the Back button below honest. */
    expect(response?.headers()["cache-control"]).toContain("no-store");
  });

  test("after signing out, Back does not bring the app back", async ({ page }) => {
    await page.goto("/holdings");
    await expect(page).toHaveURL("/holdings");

    await page.goto("/logout");
    await page.waitForURL("/", { timeout: 30_000 });

    /* The history entry for `/holdings` is still there. Going back to it must be a real request,
       and a real request now has no cookie. */
    await page.goBack();
    await page.waitForURL(/\/login/, { timeout: 30_000 });
    expect(where(page)).toBe(`/login?next=${encodeURIComponent("/holdings")}`);
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });

  test("a URL copied while signed in is useless once signed out", async ({ page }) => {
    await page.goto("/me/portfolios");
    const copied = page.url();

    await page.goto("/logout");
    await page.waitForURL("/", { timeout: 30_000 });

    await page.goto(copied);
    expect(where(page)).toBe(`/login?next=${encodeURIComponent("/me/portfolios")}`);
  });
});
