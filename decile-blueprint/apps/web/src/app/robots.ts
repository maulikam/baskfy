import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/site";

/**
 * Prompt 8 deliverable 6. docs/08 §Routes marks `/instruments/[symbol]` as "SEO-optimised (this
 * is the organic-traffic surface)", so the crawlable set is the marketing and instrument pages —
 * and the authenticated application is explicitly not, since crawling it produces nothing but
 * login redirects.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        disallow: [
          "/api/",
          "/screens",
          "/screens/",
          "/portfolios",
          "/backtests",
          "/profile",
          "/invoices",
          "/change-password",
          "/login",
          "/register",
          "/forgot-password",
          "/kitchen-sink",
          "/admin",
        ],
      },
    ],
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
