import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import {
  ServerFetchTimeoutError,
  serverFetchJson,
} from "@/lib/api/server-fetch";
import {
  SleeveUnavailableError,
  sleeveUnavailable,
} from "@/lib/api/sleeve-read";
import { auth } from "@/lib/auth";

import type { FnoInfo, FnoOvernight } from "./types";

/**
 * Server-side reads for `/options/overnight` and `/options/fno` — FO5, `docs/fno/05` §2-§3.
 *
 * **Read-only, and structurally so.** Two GET paths and no write helper: the FO settings PATCH
 * exists in the API for the desk's settings form, and the web app does not call it. `docs/fno/02`
 * Track C §4 gives the web app no route that can reach the gateway; `test_fno_readonly.py` checks
 * this file's paths.
 *
 * Null is "the API did not answer" (timeout or unreachable); a refusal, a 503 or a 500 leaves as
 * `SleeveUnavailableError`, which the pages' error boundaries render (`@/lib/options/fetch`'s
 * contract, unchanged).
 */

const TIMEOUT_MS = 4000;

async function readOrNull<T>(
  path: "/fno/overnight" | "/fno/info",
): Promise<T | null> {
  const session = await auth();
  const token = session?.accessToken;
  const url = `${serverApiOrigin()}/api/v1${path}`;
  try {
    return (await serverFetchJson({
      url,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      timeoutMs: TIMEOUT_MS,
    })) as T;
  } catch (error) {
    if (error instanceof ServerFetchTimeoutError) return null;
    const unavailable = sleeveUnavailable(error);
    if (unavailable !== null)
      throw new SleeveUnavailableError(path, unavailable);
    // An unreachable API is "nothing to show right now", said on the page — the options tab's
    // contract (`@/lib/options/fetch` readOrNull), not a swallowed failure.
    return null;
  }
}

export async function fetchFnoOvernight(): Promise<FnoOvernight | null> {
  return readOrNull<FnoOvernight>("/fno/overnight");
}

export async function fetchFnoInfo(): Promise<FnoInfo | null> {
  return readOrNull<FnoInfo>("/fno/info");
}
