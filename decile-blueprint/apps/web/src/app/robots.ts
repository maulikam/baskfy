import type { MetadataRoute } from "next";

import { PUBLIC_ROUTES, STATIC_PUBLIC_PREFIXES } from "@/lib/marketing/routes";
import { SITE_URL } from "@/lib/site";

/**
 * Prompt 8 deliverable 6, rewritten the day the gate closed.
 *
 * It used to read `allow: "/"` with a hand-kept list of disallowed prefixes, which was the same
 * shape of mistake the middleware's old `GATED_PREFIXES` was: a default-open list that a new
 * route joins silently. Now it mirrors the gate — **`Disallow: /`, then an `Allow` line for each
 * path that actually renders without a session.** Longest-match wins in every major crawler, so
 * the specific allows beat the blanket disallow, and a page that stops being public stops being
 * crawlable in the same commit that gates it.
 *
 * `/instruments/[symbol]` is the loss worth naming. docs/08 §Routes calls it "SEO-optimised (this
 * is the organic-traffic surface)", and it is now behind the gate like everything else under
 * `(app)`. Crawling it would produce nothing but login redirects, so it is not advertised. If the
 * factsheets are ever meant to be the way strangers find this product, the fix is to make
 * `/instruments` public in `src/lib/auth/public-routes.ts` — not to loosen this file, which would
 * only invite crawlers to a door that is locked.
 */
export default function robots(): MetadataRoute.Robots {
  /* `/$` anchors the landing page: without it, `Allow: /` would re-open the whole site. */
  const allow = [
    "/$",
    ...PUBLIC_ROUTES.filter((route) => route.href !== "/").map((route) => route.href as string),
    ...STATIC_PUBLIC_PREFIXES.map((prefix) => `${prefix}/`),
  ];

  return {
    rules: [{ userAgent: "*", allow: Array.from(new Set(allow)), disallow: "/" }],
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
