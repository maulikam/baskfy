import { test as setup } from "@playwright/test";
import { encode } from "next-auth/jwt";

import { mintAccessToken } from "@/lib/auth/jwt";
import { E2E_EMAIL, E2E_PUBLIC_ID } from "./helpers/auth";

/**
 * One session per run, minted rather than typed in.
 *
 * THIS USED TO DRIVE THE LOGIN FORM, AND THAT STOPPED WORKING ON 5 SEP 2026.
 * -------------------------------------------------------------------------
 * M46 (`eccef8d`, "Google sign-in replaces registration, the password, the OTP and the reset
 * link") removed the password tab this file clicked. Nothing updated the setup, so every browser
 * spec in this directory has failed at `getByRole("tab", { name: "Password" })` ever since — the
 * whole suite, not one spec, and silently, because a failed `setup` project reports as "N did not
 * run" rather than as N failures. Found on 11 Sep 2026 while gating the Portfolio Command Center.
 *
 * There is no way back to a form. Google's consent screen is a third party's page, it needs real
 * credentials, and driving it from CI would be automating somebody else's login — so the suite
 * mints the session Auth.js would have written instead.
 *
 * WHAT IS FORGED, AND WHY EACH PIECE IS SAFE
 * ------------------------------------------
 * The cookie is Auth.js's own encrypted JWT, sealed with `AUTH_SECRET`; `session.accessToken`
 * inside it is the HS256 bearer the API verifies, minted through the SAME `mintAccessToken` the
 * app uses, so its claims cannot drift from the real thing without this breaking. Both secrets
 * are the throwaway constants `playwright.config.ts` hands the servers it starts, and the account
 * is the one `baskfy_api.seed e2e` creates with a fixed `public_id`.
 *
 * `sessionEpoch` is deliberately absent. `isSessionRevoked` reads an unstamped session as
 * generation 0, which is what a freshly seeded account carries — so an omitted stamp is the
 * honest value here, not a hole. A seed that ever bumps the epoch will fail this loudly, at the
 * gate in `(app)/layout.tsx`, which is the right place to find out.
 */
export const STORAGE_STATE = "e2e/.auth/user.json";

/** `playwright.config.ts` — the same throwaway values it gives both servers. */
const AUTH_SECRET = process.env.AUTH_SECRET ?? "playwright-placeholder-secret-abcdefgh";
const JWT_SECRET = process.env.BASKFY_JWT_SECRET ?? AUTH_SECRET;

/** Auth.js v5 over plain HTTP. The cookie name is also the encryption salt. */
const COOKIE = "authjs.session-token";
const THIRTY_DAYS_SECONDS = 30 * 24 * 60 * 60;

setup("mint one session for the whole run", async ({ page }) => {
  const access = await mintAccessToken({ subject: E2E_PUBLIC_ID }, JWT_SECRET);

  const token = await encode({
    salt: COOKIE,
    secret: AUTH_SECRET,
    maxAge: THIRTY_DAYS_SECONDS,
    token: {
      sub: E2E_PUBLIC_ID,
      email: E2E_EMAIL,
      name: "End-to-end tester",
      publicId: E2E_PUBLIC_ID,
      accessToken: access.token,
      accessTokenExpiresAt: access.expiresAt,
    },
  });

  await page.context().addCookies([
    {
      name: COOKIE,
      value: token,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      secure: false,
      sameSite: "Lax",
      expires: Math.floor(Date.now() / 1000) + THIRTY_DAYS_SECONDS,
    },
  ]);

  /* Proving it rather than trusting it: `/build` is behind the gate, so landing there — and not
     on `/login` — is what says the forged cookie was accepted by the real middleware. */
  await page.goto("/build");
  await page.waitForURL(/\/build/, { timeout: 60_000 });
  await page.context().storageState({ path: STORAGE_STATE });
});
