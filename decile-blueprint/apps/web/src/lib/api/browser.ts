"use client";

import { signOut } from "next-auth/react";

import { createBaskfyClient, type BaskfyClient } from "@baskfy/api-client";

import { apiOrigin } from "@/lib/api/config";

/**
 * The API client for client components — TanStack Query's fetcher.
 *
 * The access token is read from Auth.js's session endpoint and cached until shortly before it
 * expires. docs/11 §Security pins a 15-minute access token, so "cached" means minutes, and the
 * refresh is a single request rather than one per query.
 *
 * Read *per request* rather than captured once: a token that expires mid-session would otherwise
 * turn every subsequent query into a 401 until the page reloaded.
 *
 * Every request carries an AbortSignal timeout (AUDIT 4.5). The budget matches the RSC default
 * unless `NEXT_PUBLIC_BASKFY_BROWSER_FETCH_TIMEOUT_MS` overrides it.
 */

interface SessionPayload {
  accessToken?: string;
  accessTokenExpiresAt?: number;
}

/** Refresh this many milliseconds before the token actually expires, to cover clock skew. */
const REFRESH_MARGIN_MS = 60_000;

const BROWSER_FETCH_TIMEOUT_MS: number = (() => {
  const raw = process.env.NEXT_PUBLIC_BASKFY_BROWSER_FETCH_TIMEOUT_MS?.trim();
  if (!raw) return 2500;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
})();

let cached: { token: string; expiresAt: number } | null = null;
let inFlight: Promise<string | undefined> | null = null;

export function resetTokenCache(): void {
  cached = null;
  inFlight = null;
}

async function fetchSessionToken(): Promise<string | undefined> {
  const response = await fetch("/api/auth/session", {
    credentials: "same-origin",
    signal: AbortSignal.timeout(BROWSER_FETCH_TIMEOUT_MS),
  });
  if (!response.ok) return undefined;
  const payload = (await response.json()) as SessionPayload | null;
  if (!payload?.accessToken) return undefined;
  cached = {
    token: payload.accessToken,
    expiresAt: payload.accessTokenExpiresAt ?? Date.now() + REFRESH_MARGIN_MS,
  };
  return cached.token;
}

export async function accessToken(now: number = Date.now()): Promise<string | undefined> {
  if (cached && cached.expiresAt - REFRESH_MARGIN_MS > now) return cached.token;
  inFlight ??= fetchSessionToken().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

let client: BaskfyClient | null = null;

/**
 * Sign out and leave, on any 401 from any endpoint.
 *
 * Maulik asked for this after seeing `GET /api/v1/meta/status 401` in the console while the app
 * still rendered a signed-in shell. A dead session that leaves the UI looking authenticated is
 * worse than being thrown out: every panel shows an empty or stale figure and the page gives no
 * reason, on a product about someone's money.
 *
 * `replace`, not `assign`: the authenticated page must not stay in history as the entry Back
 * returns to. `signOut` clears the Auth.js cookie first, so a Back that reaches any protected
 * route hits `middleware.ts` with no session and is sent to sign-in — which is the "no way back
 * unless login succeeds" half of the requirement. The cookie is what enforces it; the history
 * entry alone never could.
 */
async function onUnauthorized(): Promise<void> {
  resetTokenCache();
  try {
    // `redirect: false` so the hard navigation below is the only one — letting Auth.js redirect
    // too would race two navigations and can land on the sign-in page's own history entry.
    await signOut({ redirect: false });
  } finally {
    window.location.replace("/");
  }
}

function browserTimedFetch(request: Request): Promise<Response> {
  const parent = request.signal;
  const budget = AbortSignal.timeout(BROWSER_FETCH_TIMEOUT_MS);
  const signals = AbortSignal as typeof AbortSignal & {
    any?: (signals: AbortSignal[]) => AbortSignal;
  };
  const signal =
    parent && typeof signals.any === "function" ? signals.any([parent, budget]) : budget;
  return fetch(new Request(request, { signal }));
}

export function browserApi(): BaskfyClient {
  client ??= createBaskfyClient({
    baseUrl: apiOrigin(),
    getAccessToken: () => accessToken(),
    fetch: browserTimedFetch,
    onUnauthorized: () => {
      void onUnauthorized();
    },
  });
  return client;
}
