"use client";

import { apiOrigin } from "@/lib/api/config";
import { accessToken } from "@/lib/api/browser";

/**
 * Instrument typeahead — docs/07 §Instruments: `GET /instruments?search=…&limit=…`.
 *
 * Prompt 8 wired the ⌘K palette to this path before the endpoint existed; Prompt 10 built it, and
 * the wiring did not have to change. The `not-implemented` outcome is kept rather than deleted:
 * the web app and the API deploy separately, and a browser holding an older tab against a newer
 * API — or a newer bundle against an API that has not rolled yet — should say "not available
 * here" instead of "search failed". A documented RFC 9457 `404 not-found` is what distinguishes
 * them.
 *
 * Still a hand-written fetch rather than a generated client call: this runs in the browser on
 * every keystroke, and it wants an `AbortSignal` and a plain result union rather than
 * `openapi-fetch`'s `{ data, error }`. The shape it parses is the generated `InstrumentHitOut`.
 */

export interface InstrumentHit {
  symbol: string;
  name: string;
}

export type InstrumentSearchOutcome =
  | { status: "ok"; hits: InstrumentHit[] }
  | { status: "not-implemented" }
  | { status: "failed" };

const DEFAULT_LIMIT = 8;

export async function searchInstruments(
  query: string,
  signal?: AbortSignal,
  limit = DEFAULT_LIMIT,
): Promise<InstrumentSearchOutcome> {
  const url = new URL(`${apiOrigin()}/api/v1/instruments`);
  url.searchParams.set("search", query);
  url.searchParams.set("limit", String(limit));

  const token = await accessToken();
  try {
    const response = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: signal ?? null,
    });
    if (response.status === 404) return { status: "not-implemented" };
    if (!response.ok) return { status: "failed" };
    const payload = (await response.json()) as { data?: InstrumentHit[] } | InstrumentHit[];
    const hits = Array.isArray(payload) ? payload : (payload.data ?? []);
    return { status: "ok", hits };
  } catch {
    return { status: "failed" };
  }
}
