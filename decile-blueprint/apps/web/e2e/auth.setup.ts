import { test as setup } from "@playwright/test";

import { E2E_EMAIL, E2E_PASSWORD } from "./helpers/auth";

/**
 * One sign-in per run, saved to disk and reused by every project.
 *
 * This exists because the login gate is closed by default now: a spec that opens `/build` from a
 * cold browser lands on `/login`, and the *interesting* thing it was testing never runs. Doing it
 * here rather than in each spec's `beforeEach` also takes a full password round trip (Argon2id,
 * even at the suite's reduced cost) off every single test.
 *
 * Specs that must start signed out say so explicitly — `test.use({ storageState: SIGNED_OUT })`.
 */
export const STORAGE_STATE = "e2e/.auth/user.json";

setup("sign in once for the whole run", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(E2E_EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  /* `/build` is where a sign-in with no `?next=` lands. Waiting for it rather than for the
     network is what proves the credential actually worked. */
  await page.waitForURL(/\/build/, { timeout: 60_000 });
  await page.context().storageState({ path: STORAGE_STATE });
});
