import "server-only";

import type {
  IndexDashboardOut,
  ListingsPage,
  MarketHealthHistoryOut,
  MarketHealthOut,
} from "@decile/api-client";

import { apiOrigin } from "@/lib/api/config";

/**
 * Server-side reads for the market-data surfaces — docs/07 §"Market data surfaces".
 *
 * A plain `fetch` rather than the generated client, for the same reason `lib/instrument/fetch.ts`
 * is: docs/08 §Routes marks `/dashboard` "RSC, **revalidate on `data_version`**", and that needs
 * Next's `next: { tags, revalidate }` on the request, which `openapi-fetch` owns. The response
 * *types* are still the generated ones, so the contract stays as checked as everywhere else.
 *
 * The tag is shared with the instrument pages: one `revalidateTag` at publish invalidates every
 * page that reads published data, which is what "revalidate on `data_version`" means when the
 * version is not in the URL. `POST /api/revalidate` is the trigger. `docs/11a` §6.
 */

export { FACTSHEET_TAG as PUBLISHED_DATA_TAG } from "@/lib/instrument/fetch";

const FALLBACK_REVALIDATE_SECONDS = 3600;
const TAG = "factsheet";

export class MarketDataUnavailable extends Error {}

async function readJson(path: string, search: Record<string, string> = {}): Promise<unknown> {
  const url = new URL(`${apiOrigin()}/api/v1${path}`);
  for (const [key, value] of Object.entries(search)) url.searchParams.set(key, value);

  const response = await fetch(url, {
    next: { tags: [TAG], revalidate: FALLBACK_REVALIDATE_SECONDS },
  });
  if (!response.ok) throw new MarketDataUnavailable(`${path} responded ${response.status}`);
  return response.json();
}

export async function fetchIndexDashboard(): Promise<IndexDashboardOut> {
  return (await readJson("/indices/dashboard")) as IndexDashboardOut;
}

export async function fetchMarketHealth(universe: string): Promise<MarketHealthOut> {
  return (await readJson("/market-health", { universe })) as MarketHealthOut;
}

export async function fetchMarketHealthHistory(
  universe: string,
  from: string,
): Promise<MarketHealthHistoryOut> {
  return (await readJson("/market-health/history", {
    universe,
    from,
  })) as MarketHealthHistoryOut;
}

export interface ListingQuery {
  cursor?: string | undefined;
  series?: string | undefined;
  search?: string | undefined;
}

export async function fetchListings(query: ListingQuery): Promise<ListingsPage> {
  const search: Record<string, string> = {};
  if (query.cursor) search.cursor = query.cursor;
  if (query.series) search.series = query.series;
  if (query.search) search.search = query.search;
  return (await readJson("/listings", search)) as ListingsPage;
}
