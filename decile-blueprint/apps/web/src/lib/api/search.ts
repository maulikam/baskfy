"use client";

import type { CatalogHitOut, CatalogSearchOut } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * The ⌘K palette's one call — `GET /search?q=&limit=`.
 *
 * Replaces `lib/api/instruments.ts`'s `searchInstruments` as the palette's fetcher.
 * `baskfynavrefactorreport` §F11: "One global search should cover stocks, indices, baskets, and
 * screens." That module stays where it is — the instrument typeahead is still the instrument
 * typeahead, and other surfaces may want it — but the palette no longer calls it.
 *
 * Still a hand-written `fetch` rather than the generated client, for the reason
 * `lib/api/instruments.ts` gives: this runs in the browser on every keystroke, and it wants an
 * `AbortSignal` and a plain result union rather than `openapi-fetch`'s `{ data, error }`. The
 * shapes it parses are the generated `CatalogSearchOut` / `CatalogHitOut`.
 *
 * The `not-implemented` outcome is carried over from that module and is not decoration: the web
 * app and the API deploy separately, so a browser holding a newer bundle against an API that has
 * not rolled yet should say "not available here" rather than "search failed". A documented
 * RFC 9457 `404 not-found` is what distinguishes them.
 */

export type CatalogSearchOutcome =
  | { status: "ok"; query: string; hits: readonly CatalogHitOut[] }
  | { status: "not-implemented" }
  | { status: "failed" };

/** Hits **per kind**, matching the API's own default. Five fills the dialog without scrolling. */
const DEFAULT_LIMIT = 5;

export async function searchCatalog(
  query: string,
  signal?: AbortSignal,
  limit = DEFAULT_LIMIT,
): Promise<CatalogSearchOutcome> {
  const url = new URL(`${apiOrigin()}/api/v1/search`);
  url.searchParams.set("q", query);
  url.searchParams.set("limit", String(limit));

  const token = await accessToken();
  try {
    const response = await fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: signal ?? null,
    });
    if (response.status === 404) return { status: "not-implemented" };
    if (!response.ok) return { status: "failed" };
    const payload = (await response.json()) as CatalogSearchOut;
    return { status: "ok", query: payload.query, hits: payload.data ?? [] };
  } catch {
    /*
     * An aborted request lands here too, which is correct for this caller: the palette discards
     * any outcome whose query is not the one it is currently showing, so a `failed` from an abort
     * is never rendered. Distinguishing `AbortError` would buy a branch nobody reads.
     */
    return { status: "failed" };
  }
}
