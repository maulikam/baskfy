import "server-only";

import { createBaskfyClient } from "@baskfy/api-client";

import { serverApiOrigin } from "@/lib/api/config";
import { timedFetch } from "@/lib/api/server-fetch";
import { currentTraceparent } from "@/lib/api/trace";
import { auth } from "@/lib/auth";
import {
  parseCandidates,
  type OverlapCandidates,
  type OverlapScope,
} from "@/lib/overlap/candidates";

/**
 * `GET /overlap` — the day's candidates across the sleeves, one row per stock, from what the
 * scans stored. Read-only, and nothing is re-run.
 *
 * This is the read that replaces the symbols-only membership `/build/overlap` used to compute
 * in this app from three separate page fetches: same rows, same sessions, plus the facts a
 * person was being sent to each hub for — each strategy's own word on the row, whether that
 * strategy could act on it, the swing feed's catalyst link and earnings date, and the live mark.
 *
 * `null` means the API did not answer (a refusal, a timeout, a 5xx). The page keeps its symbol
 * matrix and says the facts could not be read, rather than rendering that as "no candidates".
 *
 * **Its own budget.** This read runs beside `fetchOverlapSources`, which re-runs up to three
 * screens on the API in the same second; under the shared 2.5 s hop budget it lost that race
 * often enough that the table blinked out on staging (25 Sep 2026). Eight seconds is the
 * ceiling a person will wait for a table that is the point of the page; the matrix still
 * streams meanwhile, and a miss still says so.
 */
export const CANDIDATES_TIMEOUT_MS = 8000;

export async function fetchCandidates(scope: OverlapScope): Promise<OverlapCandidates | null> {
  const session = await auth();
  const token = session?.accessToken;
  const api = createBaskfyClient({
    baseUrl: serverApiOrigin(),
    fetch: timedFetch(CANDIDATES_TIMEOUT_MS),
    ...(token ? { getAccessToken: () => token } : {}),
    getTraceparent: currentTraceparent,
  });
  try {
    const { data } = await api.GET("/api/v1/overlap", { params: { query: { scope } } });
    if (!data) return null;
    return parseCandidates(data);
  } catch {
    return null;
  }
}
