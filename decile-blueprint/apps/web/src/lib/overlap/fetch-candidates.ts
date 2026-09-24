import "server-only";

import { serverApi } from "@/lib/api/server";
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
 */
export async function fetchCandidates(scope: OverlapScope): Promise<OverlapCandidates | null> {
  const api = await serverApi();
  try {
    const { data } = await api.GET("/api/v1/overlap", { params: { query: { scope } } });
    if (!data) return null;
    return parseCandidates(data);
  } catch {
    return null;
  }
}
