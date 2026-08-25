import type { MetadataRoute } from "next";

import { POST_META_BY_DATE } from "@/lib/marketing/post-meta";
import { PUBLIC_ROUTES } from "@/lib/marketing/routes";
import { SITE_URL } from "@/lib/site";

/**
 * The crawlable surface — and since the gate closed, it is a short one.
 *
 * It used to enumerate every instrument on the register, because docs/08 §Routes marks
 * `/instruments/[symbol]` "SEO-optimised (this is the organic-traffic surface)" and a sitemap is
 * how a crawler finds several thousand of them. `/instruments` is now behind the login gate along
 * with the rest of `(app)`, so those URLs answer a 307 to `/login`. A sitemap of redirects is not
 * a smaller sitemap; it is a sitemap that teaches a crawler to stop trusting this one. The
 * enumeration is gone rather than filtered, and `fetchListings` is no longer called here at all —
 * building the sitemap no longer needs the API to be up.
 *
 * What remains is what is actually public: the routes in `lib/marketing/routes` and the blog. If
 * the factsheets are meant to be an acquisition surface again, re-open `/instruments` in
 * `src/lib/auth/public-routes.ts` and restore the walk from git history — in that order.
 *
 * `revalidate` stays a day. Nothing here reads a database any more, so it is now only about how
 * quickly a newly published post appears.
 */
export const revalidate = 86_400;

const STATIC_ROUTES: { path: string; priority: number; changeFrequency: "weekly" }[] =
  PUBLIC_ROUTES.map((route) => ({
    path: route.href === "/" ? "" : route.href,
    priority: route.priority,
    changeFrequency: "weekly" as const,
  }));

export default function sitemap(): MetadataRoute.Sitemap {
  const lastModified = new Date();

  return [
    ...STATIC_ROUTES.map((route) => ({
      url: `${SITE_URL}${route.path}`,
      lastModified,
      changeFrequency: route.changeFrequency,
      priority: route.priority,
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
