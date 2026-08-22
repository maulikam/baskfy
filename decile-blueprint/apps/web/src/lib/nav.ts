/**
 * The sidebar's information architecture — docs/08 §"App shell", verbatim.
 *
 *     "Collapsible left sidebar (matching the reference IA): Dashboard · Market Health · Screens ·
 *      Rebalance Tracker · Backtests · Listings — then Account (Pricing, Invoices, Profile, Change
 *      Password) — then Help (FAQ, Blog, Support)."
 *
 * Routes are the ones docs/08 §Routes and docs/01 §1 name. Several are built in later prompts;
 * `status: "planned"` marks those, and the sidebar renders them as disabled rather than as links
 * to a 404 — a nav item that lies about where it goes is worse than one that admits it is not
 * ready. `src/app/__tests__/nav.test.ts` pins the groups and their order against this comment.
 */

import type { Route } from "next";

export type NavStatus = "ready" | "planned";

interface NavItemBase {
  label: string;
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
  | "check-check";

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: null,
    items: [
      { href: "/dashboard", label: "Dashboard", icon: "layout-dashboard", status: "ready" },
      { href: "/market-health", label: "Market Health", icon: "activity", status: "ready" },
      { href: "/screens", label: "Screens", icon: "table-2", status: "ready" },
      { href: "/portfolios", label: "Rebalance Tracker", icon: "scale", status: "ready" },
      // M22. The desk's output, read-only: what the strategy wants today and what was last
      // planned. Not in docs/08's IA, which predates the merge -- see MERGE-PROMPTS.md §M22.
      { href: "/baskets", label: "Baskets", icon: "briefcase", status: "ready" },
      { href: "/backtests", label: "Backtests", icon: "history", status: "ready" },
      { href: "/listings", label: "Listings", icon: "list", status: "ready" },
    ],
  },
  // M26. The desk's read-only surfaces, gathered rather than scattered through the primary
  // group. docs/08's IA predates the merge and describes the screener alone; these five are the
  // desk's own console pages, and grouping them says what they are -- the record of a portfolio
  // that is actually being traded, as against the screener's analysis of a market.
  //
  // Every one is read-only. Execution stays in the desk console (MERGE-PROMPTS.md §M26, D3).
  {
    label: "Desk",
    items: [
      { href: "/performance", label: "Performance", icon: "trending-up", status: "ready" },
      { href: "/holdings", label: "Holdings", icon: "wallet", status: "ready" },
      { href: "/tradebook", label: "Trades", icon: "arrow-left-right", status: "ready" },
      { href: "/regime", label: "Market stance", icon: "gauge", status: "ready" },
      { href: "/reconcile", label: "Plan vs fills", icon: "check-check", status: "ready" },
    ],
  },
  {
    label: "Account",
    items: [
      { href: "/pricing", label: "Pricing", icon: "tag", status: "ready" },
      { href: "/invoices", label: "Invoices", icon: "receipt", status: "ready" },
      { href: "/profile", label: "Profile", icon: "user", status: "ready" },
      { href: "/change-password", label: "Change Password", icon: "key-round", status: "ready" },
    ],
  },
  {
    label: "Help",
    items: [
      { href: "/faq", label: "FAQ", icon: "circle-help", status: "ready" },
      { href: "/blog", label: "Blog", icon: "newspaper", status: "ready" },
      { href: "/support", label: "Support", icon: "life-buoy", status: "ready" },
    ],
  },
] as const;

/** Every nav destination, flattened — used by tests and by the command palette. */
export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((group) => group.items);
