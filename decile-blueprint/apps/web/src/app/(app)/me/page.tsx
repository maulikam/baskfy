import { redirect } from "next/navigation";

/**
 * `/me` → `/profile`.
 *
 * PORTFOLIO_REDESIGN.md §2 emptied Me of investment data: it is profile, brokers, subscription
 * and security now (`SECTION_TABS.me`), and the money moved to `/portfolio`. This used to send
 * people to `/me/investments`, which is a redirect into `/portfolio/overview` today.
 *
 * Kept as a page redirect rather than promoted into `next.config.ts`. A config redirect would
 * shadow this file — Next runs `redirects()` before the filesystem routes — so the two cannot
 * coexist, and `scripts/check-shadowed-routes.mjs` enforces that. Keeping the page is the
 * reversible half of the choice: when Me becomes a real hub with its own tab row, this file
 * grows into it. A config entry would have to be deleted first.
 */
export default function MeIndexPage() {
  redirect("/profile");
}
