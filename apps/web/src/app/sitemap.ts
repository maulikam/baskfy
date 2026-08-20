import type { MetadataRoute } from "next";

import { fetchListings } from "@/lib/market/fetch";
import { SITE_URL } from "@/lib/site";

/**
 * The crawlable surface — docs/08 §Routes marks `/instruments/[symbol]` "SEO-optimised (this is
 * the organic-traffic surface)", and a sitemap is how a crawler finds several thousand of them.
 *
 * This became possible with Prompt 11: enumerating instruments needs an endpoint that enumerates
 * instruments, and until `/listings` existed there was none — `/instruments?search=` requires a
 * search term. `docs/10a` §6 recorded the gap; this closes it.
 *
 * Bounded on purpose. A sitemap is capped at 50,000 URLs by the protocol and this walks a page at
 * a time, so the cap here is about *build time*, not about correctness: `MAX_PAGES` pages of 100
 * covers the whole NSE register with room to spare, and if the register ever outgrew it the
 * remainder would still be reachable through `/listings` and the results tables rather than
 * silently absent from both.
 *
 * If the API is unreachable the sitemap degrades to the static routes rather than failing the
 * build. A missing sitemap costs discovery; a failed build costs the deploy.
 */
export const revalidate = 86_400;

const MAX_PAGES = 60;

const STATIC_ROUTES: { path: string; priority: number; changeFrequency: "weekly" | "daily" }[] = [
  { path: "", priority: 1, changeFrequency: "weekly" },
  { path: "/dashboard", priority: 0.8, changeFrequency: "daily" },
  { path: "/market-health", priority: 0.8, changeFrequency: "daily" },
  { path: "/listings", priority: 0.6, changeFrequency: "daily" },
];

async function instrumentSymbols(): Promise<string[]> {
  const symbols: string[] = [];
  let cursor: string | undefined;

  try {
    for (let page = 0; page < MAX_PAGES; page += 1) {
      const result = await fetchListings(cursor ? { cursor } : {});
      symbols.push(...result.data.map((row) => row.symbol));
      if (!result.next_cursor) break;
      cursor = result.next_cursor;
    }
  } catch {
    // Reported by returning what was gathered: a short sitemap is a degraded sitemap, and the
    // instrument pages stay crawlable through `robots.ts` and every results table meanwhile.
    return symbols;
  }
  return symbols;
}

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const lastModified = new Date();
  const symbols = await instrumentSymbols();

  return [
    ...STATIC_ROUTES.map((route) => ({
      url: `${SITE_URL}${route.path}`,
      lastModified,
      changeFrequency: route.changeFrequency,
      priority: route.priority,
    })),
    ...symbols.map((symbol) => ({
      url: `${SITE_URL}/instruments/${encodeURIComponent(symbol)}`,
      lastModified,
      changeFrequency: "daily" as const,
      priority: 0.7,
    })),
  ];
}
