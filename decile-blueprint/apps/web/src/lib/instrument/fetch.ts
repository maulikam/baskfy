import "server-only";

import type { FactsheetOut, InstrumentHistoryOut } from "@decile/api-client";

import { apiOrigin } from "@/lib/api/config";

/**
 * Server-side reads for `/instruments/[symbol]` — docs/07 §Instruments.
 *
 * A plain `fetch` rather than the generated client for one reason: **ISR**. docs/08 §Routes marks
 * this route `ISR` and Prompt 10 asks for it to be "revalidated on `data_version`". That needs
 * Next's `next: { tags, revalidate }` on the request, and `openapi-fetch` owns the init object it
 * passes to `fetch`. The response *types* still come from the generated client, so the contract is
 * as checked as everywhere else — only the transport is hand-rolled.
 *
 * The tag is what makes "revalidated on `data_version`" true rather than aspirational: the
 * nightly publish step (docs/09 §Schedule) calls `POST /api/revalidate` when it bumps the
 * version, every factsheet is invalidated at once, and the next request re-renders. The
 * time-based `revalidate` below is the backstop for the night that call does not arrive.
 */

/** One hour. A factsheet changes once a night; this only matters if the webhook is missed. */
const FALLBACK_REVALIDATE_SECONDS = 3600;

/** Every factsheet carries this tag, so one `revalidateTag` call invalidates all of them. */
export const FACTSHEET_TAG = "factsheet";

export class FactsheetNotFound extends Error {}

async function readJson(path: string, search?: Record<string, string>): Promise<unknown> {
  const url = new URL(`${apiOrigin()}/api/v1${path}`);
  for (const [key, value] of Object.entries(search ?? {})) url.searchParams.set(key, value);

  const response = await fetch(url, {
    next: { tags: [FACTSHEET_TAG], revalidate: FALLBACK_REVALIDATE_SECONDS },
  });
  if (response.status === 404) throw new FactsheetNotFound(path);
  if (!response.ok) throw new Error(`${path} responded ${response.status}`);
  return response.json();
}

export async function fetchFactsheet(symbol: string): Promise<FactsheetOut> {
  return (await readJson(`/instruments/${encodeURIComponent(symbol)}`)) as FactsheetOut;
}

export async function fetchHistory(
  symbol: string,
  field: string,
): Promise<InstrumentHistoryOut | null> {
  try {
    return (await readJson(`/instruments/${encodeURIComponent(symbol)}/history`, {
      field,
    })) as InstrumentHistoryOut;
  } catch {
    // A missing sparkline is not a missing page. The card renders its value and the Sparkline
    // draws its "not enough history" rule — which is the same thing it does for a young listing.
    return null;
  }
}
