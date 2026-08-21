import type { MetadataRoute } from "next";

import { fetchListings } from "@/lib/market/fetch";
import { POST_META_BY_DATE } from "@/lib/marketing/post-meta";
import { PUBLIC_ROUTES } from "@/lib/marketing/routes";
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

/**
 * Prompt 18 §4 widened this from four hand-written entries to the whole public surface. The list
 * is `lib/marketing/routes`, so a legal page or a content page cannot ship unlisted — a page that
 * is in no sitemap and in no footer is a page nobody will read.
 *
 * The three data-backed routes change daily; everything else changes when we deploy.
 */
const DAILY = new Set(["/dashboard", "/market-health", "/listings"]);

const STATIC_ROUTES: { path: string; priority: number; changeFrequency: "weekly" | "daily" }[] =
  PUBLIC_ROUTES.map((route) => ({
    path: route.href === "/" ? "" : route.href,
    priority: route.priority,
    changeFrequency: DAILY.has(route.href) ? ("daily" as const) : ("weekly" as const),
  }));

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
    /* Prompt 18 §4: "sitemap including every instrument page". */
    ...symbols.map((symbol) => ({
      url: `${SITE_URL}/instruments/${encodeURIComponent(symbol)}`,
      lastModified,
      changeFrequency: "daily" as const,
      priority: 0.7,
    })),
    /* A post's `lastModified` is its publication date, not the build time: telling a crawler that
       a two-month-old essay changed this morning is how a sitemap stops being believed. */
    ...POST_META_BY_DATE.map((post) => ({
      url: `${SITE_URL}/blog/${post.slug}`,
      lastModified: new Date(`${post.date}T00:00:00+05:30`),
      changeFrequency: "yearly" as const,
      priority: 0.6,
    })),
  ];
}
