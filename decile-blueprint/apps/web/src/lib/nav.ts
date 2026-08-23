/**
 * The sidebar's information architecture.
 *
 * docs/08 §"App shell" stated it verbatim, and until M36 this file quoted it back:
 *
 *     "Collapsible left sidebar (matching the reference IA): Dashboard · Market Health · Screens ·
 *      Rebalance Tracker · Backtests · Listings — then Account (Pricing, Invoices, Profile, Change
 *      Password) — then Help (FAQ, Blog, Support)."
 *
 * **The order still is that. The words are not.** Maulik's instruction of 23 Aug 2026 was to
 * relabel the product for somebody who does not already speak the vocabulary, and the sidebar is
 * the first place a beginner meets it: "Rebalance Tracker", "Market Health" and "Plan vs fills"
 * are all terms you have to already know to choose between. Every label now comes from
 * `lib/vocabulary`, which keeps the professional name beside the plain one and shows it in the
 * tooltip — so nothing was renamed away, only put in the second position.
 *
 * The *structure* is untouched, because that part of docs/08 is an observation about how the
 * reference product organises itself rather than a choice of words, and `nav.test.ts` still pins
 * it route by route. Renaming is reversible in one file; reordering is a different decision and
 * was not asked for (`docs/DECISIONS-MERGE.md` §M36.1).
 *
 * Routes are the ones docs/08 §Routes and docs/01 §1 name. Several are built in later prompts;
 * `status: "planned"` marks those, and the sidebar renders them as disabled rather than as links
 * to a 404 — a nav item that lies about where it goes is worse than one that admits it is not
 * ready. `src/lib/__tests__/nav.test.ts` pins the groups and their order against this comment.
 */
import type { Route } from "next";

import { PAGES, type PagePath } from "@/lib/vocabulary";

export type NavStatus = "ready" | "planned";

interface NavItemBase {
  /** The plain-English name, from `lib/vocabulary`. */
  label: string;
  /** One sentence saying what the destination answers. The sidebar shows it on hover. */
  blurb: string;
  /** The professional name this label replaced, where it replaced one. Also shown on hover. */
  formerly?: string;
  /** lucide-react icon name, resolved in the sidebar so this module stays serialisable. */
  icon: NavIconName;
}

/**
 * A destination that exists.
 *
 * `href` is Next's `Route`, not `string`, so `typedRoutes` checks it: marking an item ready before
 * its page exists becomes a compile error rather than a 404 someone finds in staging.
 */
export interface ReadyNavItem extends NavItemBase {
  status: "ready";
  href: Route;
}

/** A destination a later prompt delivers. Never rendered as a link, so its href is not a route. */
export interface PlannedNavItem extends NavItemBase {
  status: "planned";
  href: string;
  /** Which prompt delivers it, so a disabled item explains itself in a tooltip. */
  arrivesIn: string;
}

export type NavItem = ReadyNavItem | PlannedNavItem;

export interface NavGroup {
  /** `null` renders the group without a heading — the primary group has none in the reference. */
  label: string | null;
  items: NavItem[];
}

export type NavIconName =
  | "layout-dashboard"
  | "activity"
  | "table-2"
  | "scale"
  | "briefcase"
  | "history"
  | "list"
  | "tag"
  | "receipt"
  | "user"
  | "key-round"
  | "circle-help"
  | "newspaper"
  | "life-buoy"
  | "trending-up"
  | "wallet"
  | "arrow-left-right"
  | "gauge"
  | "check-check"
  | "plug"
  | "compass"
  | "bookmark"
  | "landmark"
  | "percent";

/**
 * Build a ready item from its route, so the label, the blurb and the professional name can never
 * drift from what the page itself renders. `PagePath` makes a typo a compile error.
 */
function page(href: PagePath & Route, icon: NavIconName): ReadyNavItem {
  const entry = PAGES[href];
  return {
    href,
    label: entry.title,
    blurb: entry.blurb,
    ...("formerly" in entry ? { formerly: entry.formerly } : {}),
    icon,
    status: "ready",
  };
}

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: null,
    items: [
      page("/dashboard", "layout-dashboard"),
      page("/market-health", "activity"),
      page("/screens", "table-2"),
      page("/portfolios", "scale"),
      // SC5. Public curated-basket catalog (`/api/v1/explore`). Soft-coexists with M22
      // `/baskets` (desk MomentumScan operator view) — DECISIONS-SC SC5.
      page("/explore", "compass"),
      // SC6. Investor surfaces — investments / watchlist; fees sits in Account.
      page("/investments", "landmark"),
      page("/watchlist", "bookmark"),
      // M22. The desk's output, read-only: what the strategy wants today and what was last
      // planned. Not in docs/08's IA, which predates the merge -- see MERGE-PROMPTS.md §M22.
      page("/baskets", "briefcase"),
      page("/backtests", "history"),
      page("/listings", "list"),
    ],
  },
  // M26. The desk's read-only surfaces, gathered rather than scattered through the primary
  // group. docs/08's IA predates the merge and describes the screener alone; these five are the
  // desk's own console pages, and grouping them says what they are -- the record of a portfolio
  // that is actually being traded, as against the screener's analysis of a market.
  //
  // M36 renamed the group from "Desk", which named the machinery rather than the money. "Real
  // money" is the distinction that actually matters to a reader deciding which half of the app
  // they are looking at: everything above this line is analysis, everything in it is a position
  // somebody holds.
  //
  // Every one is read-only. Execution stays in the desk console (MERGE-PROMPTS.md §M26, D3).
  {
    label: "Real money",
    items: [
      page("/performance", "trending-up"),
      page("/holdings", "wallet"),
      page("/tradebook", "arrow-left-right"),
      page("/regime", "gauge"),
      page("/reconcile", "check-check"),
    ],
  },
  {
    label: "Account",
    items: [
      page("/pricing", "tag"),
      page("/invoices", "receipt"),
      page("/fees", "percent"),
      page("/profile", "user"),
      // M41: broker connect grid (P5.8). Live OAuth stays D3-gated; the page itself is ready.
      page("/brokers", "plug"),
      page("/change-password", "key-round"),
    ],
  },
  {
    label: "Help",
    items: [page("/faq", "circle-help"), page("/blog", "newspaper"), page("/support", "life-buoy")],
  },
] as const;

/** Every nav destination, flattened — used by tests and by the command palette. */
export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((group) => group.items);
