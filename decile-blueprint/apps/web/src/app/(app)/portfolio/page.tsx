import { redirect } from "next/navigation";

/**
 * `/portfolio` → `/portfolio/overview`.
 *
 * Overview is the section's default landing tab (PORTFOLIO_REDESIGN.md §2), so the bare section
 * path has to land somewhere rather than 404. This is a page redirect and not a `next.config.ts`
 * entry on purpose: `redirects()` runs before the filesystem routes, so a config redirect at
 * `/portfolio` would make this file unreachable — and `scripts/check-shadowed-routes.mjs` would
 * (correctly) refuse the pair. The same reasoning keeps `/me/page.tsx` a page redirect.
 */
export default function PortfolioIndexPage() {
  redirect("/portfolio/overview");
}
