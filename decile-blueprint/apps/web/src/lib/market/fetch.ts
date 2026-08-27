import "server-only";

import type {
  IndexDashboardOut,
  ListingsPage,
  MarketHealthHistoryOut,
  MarketHealthOut,
} from "@baskfy/api-client";

import { serverApiOrigin } from "@/lib/api/config";

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

/**
 * The API's own word for "I have nothing trustworthy to show you yet".
 *
 * `docs/07`'s problem catalogue answers `503 pipeline-degraded` when no `pipeline_run` has been
 * published, which is a *state*, not a fault: the pipeline has not run, or last night's run did
 * not pass its gate. docs/11 §Reliability calls the correct response "graceful degradation" —
 * serve the page, say so.
 *
 * Only 503. A 500 is a bug and must stay loud; a 404 means the route moved and must stay loud.
 * Swallowing those would turn every backend defect into a quiet empty state, which is the
 * failure mode this distinction exists to avoid.
 */
const DEGRADED_STATUS = 503;

async function readJson(path: string, search: Record<string, string> = {}): Promise<unknown> {
  const url = new URL(`${serverApiOrigin()}/api/v1${path}`);
  for (const [key, value] of Object.entries(search)) url.searchParams.set(key, value);

  const response = await fetch(url, {
    next: { tags: [TAG], revalidate: FALLBACK_REVALIDATE_SECONDS },
  });
  if (!response.ok) throw new MarketDataUnavailable(`${path} responded ${response.status}`);
  return response.json();
}

/**
 * Read, or answer `null` when the pipeline has nothing published.
 *
 * The throwing {@link readJson} stays, and so does every caller that wants it: a page whose whole
 * reason to exist is the data may reasonably fail. This is for the pages that can say something
 * useful without it.
 */
async function readJsonOrDegraded(
  path: string,
  search: Record<string, string> = {},
  // `unknown`, not `unknown | null`: `unknown` already admits null, and spelling the union out
  // reads as a promise the type does not make. The nullability that matters is on the exported
  // wrappers, where it is `IndexDashboardOut | null` and does mean something.
): Promise<unknown> {
  try {
    return await readJson(path, search);
  } catch (error) {
    if (error instanceof MarketDataUnavailable && error.message.endsWith(`${DEGRADED_STATUS}`)) {
      return null;
    }
    throw error;
  }
}

export async function fetchIndexDashboard(): Promise<IndexDashboardOut> {
  return (await readJson("/indices/dashboard")) as IndexDashboardOut;
}

/**
 * The index dashboard, or `null` while the pipeline has published nothing.
 *
 * `/market/today` used to call the throwing variant, so a brand-new deployment — where no
 * pipeline has ever run — answered a full-page "Application error: a server-side exception has
 * occurred", digest and all. The API was behaving correctly and saying so precisely; the page
 * turned a 503 into a 500.
 */
export async function fetchIndexDashboardOrDegraded(): Promise<IndexDashboardOut | null> {
  return (await readJsonOrDegraded("/indices/dashboard")) as IndexDashboardOut | null;
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
