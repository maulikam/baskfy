import { expect, type Page } from "@playwright/test";

/**
 * Signing in, in one place — because since the gate closed, nearly every spec needs it.
 *
 * The suite used to hit `/build`, `/listings`, `/dashboard` and the rest straight from a cold
 * browser, and they rendered. They do not any more: everything outside the marketing pages
 * redirects to `/login`. Rather than paste a sign-in helper into each spec (there were eight
 * near-identical copies), `auth.setup.ts` signs in once per run and every project inherits the
 * cookie through `storageState`. This helper stays for the specs that need to sign in *as part of
 * what they are testing*.
 */

/** Seeded by `baskfy_api.seed e2e` — `E2E_EMAIL` / `E2E_PASSWORD` in `seed.py`. */
export const E2E_EMAIL = "e2e@example.com";
export const E2E_PASSWORD = "e2e-suite-password";

/** Where a sign-in with no `?next=` lands (`src/app/actions/auth.ts`). */
export const DEFAULT_DESTINATION = "/build";

/**
 * The storage state to give a spec that must start signed out.
 *
 * Not `as const`: Playwright's `StorageState` wants mutable arrays, and a readonly literal is
 * rejected at the `test.use` call site rather than here, where the reason would be visible.
 */
export const SIGNED_OUT: { cookies: []; origins: [] } = { cookies: [], origins: [] };

export async function signIn(
  page: Page,
  { email = E2E_EMAIL, password = E2E_PASSWORD, expectUrl = /\/build/ } = {},
): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(expectUrl, { timeout: 30_000 });
}

/** Sign out the way the user menu does, and wait for the landing page. */
export async function signOut(page: Page): Promise<void> {
  await page.goto("/logout");
  await page.waitForURL("/", { timeout: 30_000 });
  await expect(page).toHaveURL("/");
}
