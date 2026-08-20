import { expect, test, type Page } from "@playwright/test";

import { clearInbox, codeFrom, tokenFrom, waitForEmail } from "./mailpit";

/**
 * Prompt 12's third acceptance criterion, end to end:
 *
 *     "Playwright e2e covering register → verify → login → change password → delete account."
 *
 * Nothing is stubbed. The account is created through `POST /auth/register`, the confirmation link
 * is read out of **mailpit** (Prompt 12 §3), the password is verified with Argon2id by the API,
 * and the deletion is the real DPDP soft-delete. Each run uses a fresh address so the suite can be
 * re-run against the same database without a reset.
 */

const PASSWORD = "correct horse battery staple";
const NEW_PASSWORD = "a completely different passphrase";

/** A distinct address per test, so a re-run is not a duplicate registration. */
function freshEmail(label: string): string {
  const unique = `${Date.now().toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
  // RFC 2606's documentation domain. The `.test` TLD is special-use (RFC 6761) and
  // `email-validator` refuses it, so an address there could never be registered. `docs/12a` §12.
  return `e2e-${label}-${unique}@example.com`;
}

async function registerAccount(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/register");
  // `getByLabel("Email")` also matches the marketing checkbox's label; the role narrows it.
  await page.getByRole("textbox", { name: "Email" }).fill(email);
  await page.getByLabel(/^Password/).fill(password);
  await page.getByRole("checkbox", { name: /accept the Terms/i }).check();
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText(/we have sent it an email/i)).toBeVisible({ timeout: 15_000 });
}

async function signInWithPassword(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/login");
  await page.getByRole("tab", { name: "Password" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test.describe("the account lifecycle", () => {
  test.beforeEach(async ({ request }) => {
    await clearInbox(request);
  });

  test("register → verify → login → change password → delete account", async ({
    page,
    request,
  }) => {
    test.slow();
    const email = freshEmail("lifecycle");

    // --- register ----------------------------------------------------------
    await registerAccount(page, email, PASSWORD);

    // --- verify ------------------------------------------------------------
    const confirmation = await waitForEmail(request, email, { subjectContains: "Confirm" });
    expect(confirmation.Text, "Prompt 12 §3: plain-text alternative").toBeTruthy();
    expect(confirmation.HTML).toBeTruthy();

    await page.goto(`/verify-email?token=${tokenFrom(confirmation)}`);
    await expect(page.getByRole("heading", { name: "Email confirmed" })).toBeVisible();

    // --- login -------------------------------------------------------------
    await signInWithPassword(page, email, PASSWORD);
    await page.waitForURL(/\/screens/, { timeout: 20_000 });

    await page.goto("/profile");
    await expect(page.getByRole("heading", { level: 1, name: "Profile" })).toBeVisible();
    await expect(page.getByText(email)).toBeVisible();
    await expect(page.getByText("Verified")).toBeVisible();

    // --- change password ---------------------------------------------------
    await page.goto("/change-password");
    await page.getByLabel("Current password").fill(PASSWORD);
    await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByLabel("Confirm new password").fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByText(/your password has been changed/i)).toBeVisible({
      timeout: 15_000,
    });

    // The old password no longer works; the new one does.
    await page.goto("/logout");
    await signInWithPassword(page, email, PASSWORD);
    await expect(page.getByText(/did not match an account/i)).toBeVisible({ timeout: 15_000 });
    await signInWithPassword(page, email, NEW_PASSWORD);
    await page.waitForURL(/\/screens/, { timeout: 20_000 });

    // --- delete account ----------------------------------------------------
    await page.goto("/profile");
    await page.getByTestId("open-delete-account").click();
    await page.getByLabel(/Type .* to confirm/).fill(email);
    await page.getByRole("button", { name: "Delete my account" }).click();

    await page.waitForURL(/\/login/, { timeout: 20_000 });
    await expect(page.getByText(/scheduled for deletion/i)).toBeVisible();

    // Deactivated: the password that worked a moment ago does not any more.
    await signInWithPassword(page, email, NEW_PASSWORD);
    await expect(page.getByText(/did not match an account/i)).toBeVisible({ timeout: 15_000 });

    // ...and the owner was told (Prompt 12 §5).
    const notice = await waitForEmail(request, email, { subjectContains: "deleted" });
    expect(notice.Text).toContain("7 days");
  });

  test("signs in with a one-time code, which is the default path", async ({ page, request }) => {
    const email = freshEmail("otp");
    await registerAccount(page, email, PASSWORD);

    await page.goto("/login");
    await page.getByRole("tab", { name: "One-time code" }).click();
    await page.getByRole("textbox", { name: "Email" }).first().fill(email);
    await page.getByRole("button", { name: "Send me a code" }).click();
    await expect(page.getByText(/we have sent it an email/i)).toBeVisible({ timeout: 15_000 });

    const message = await waitForEmail(request, email, { subjectContains: "sign-in code" });
    const code = codeFrom(message);

    await page.getByRole("textbox", { name: "Email" }).nth(1).fill(email);
    await page.getByLabel("Six-digit code").fill(code);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(/\/screens/, { timeout: 20_000 });
  });

  test("resets a forgotten password from the emailed link", async ({ page, request }) => {
    const email = freshEmail("reset");
    await registerAccount(page, email, PASSWORD);

    await page.goto("/forgot-password");
    await page.getByLabel("Email").fill(email);
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByText(/we have sent it an email/i)).toBeVisible({ timeout: 15_000 });

    const message = await waitForEmail(request, email, { subjectContains: "Reset" });
    await page.goto(`/reset-password?token=${tokenFrom(message)}`);
    await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Set new password" }).click();
    await expect(page.getByText(/your password has been changed/i)).toBeVisible({
      timeout: 15_000,
    });

    await signInWithPassword(page, email, NEW_PASSWORD);
    await page.waitForURL(/\/screens/, { timeout: 20_000 });
  });

  test("a gated route sends an anonymous visitor to sign in, and back again", async ({ page }) => {
    await page.goto("/profile");
    await expect(page).toHaveURL(/\/login\?next=%2Fprofile/);
    await expect(page.getByText(/continue to \/profile/i)).toBeVisible();
  });

  test("an off-site next parameter is not followed", async ({ page, request }) => {
    /* An open redirect is a phishing primitive; the action validates `next` again. */
    const email = freshEmail("redirect");
    await registerAccount(page, email, PASSWORD);
    await request.fetch("about:blank").catch(() => null);

    await page.goto("/login?next=https://example.com/phishing");
    await page.getByRole("tab", { name: "Password" }).click();
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();

    await page.waitForURL(/\/screens/, { timeout: 20_000 });
    expect(page.url()).not.toContain("example.com");
  });

  test("exports everything we hold, as a download", async ({ page }) => {
    const email = freshEmail("export");
    await registerAccount(page, email, PASSWORD);
    await signInWithPassword(page, email, PASSWORD);
    await page.waitForURL(/\/screens/, { timeout: 20_000 });

    await page.goto("/profile");
    const download = page.waitForEvent("download");
    await page.getByTestId("export-my-data").click();
    const file = await download;
    expect(file.suggestedFilename()).toBe("decile-account-export.json");
  });
});

test.describe("the security headers docs/11 pins", () => {
  test("every page carries a strict CSP and the rest", async ({ page }) => {
    const response = await page.goto("/login");
    const headers = response?.headers() ?? {};

    const csp = headers["content-security-policy"] ?? "";
    expect(csp, "docs/11: strict CSP with default-src 'self'").toContain("default-src 'self'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toMatch(/script-src[^;]*'nonce-/);

    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["referrer-policy"]).toBe("strict-origin-when-cross-origin");
    expect(headers["strict-transport-security"]).toContain("max-age=");
  });

  test("the page still works under that policy", async ({ page }) => {
    const violations: string[] = [];
    page.on("console", (message) => {
      if (message.text().includes("Content Security Policy")) violations.push(message.text());
    });
    await page.goto("/login");
    await expect(page.getByRole("heading", { level: 1, name: "Sign in" })).toBeVisible();
    // The theme toggle is the inline-script canary: `next-themes` writes one before hydration.
    await page.getByRole("tab", { name: "Password" }).click();
    await expect(page.getByLabel("Password")).toBeVisible();
    expect(violations, violations.join("\n")).toEqual([]);
  });

  test("the browser can still reach the API under that policy", async ({ page }) => {
    /*
     * `connect-src` is the directive most easily got wrong: a source carrying a path matches that
     * path *only*, so `http://host/api/v1` silently blocks every call to `/api/v1/anything`. This
     * loads a page whose data comes from the browser, not from the server render.
     */
    const violations: string[] = [];
    page.on("console", (message) => {
      if (message.text().includes("Content Security Policy")) violations.push(message.text());
    });

    await page.goto("/screens/exmpl0000001");
    await expect(page.getByTestId("result-count")).toHaveText("271 results", { timeout: 30_000 });
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
