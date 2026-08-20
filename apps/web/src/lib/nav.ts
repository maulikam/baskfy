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
  | "history"
  | "list"
  | "tag"
  | "receipt"
  | "user"
  | "key-round"
  | "circle-help"
  | "newspaper"
  | "life-buoy";

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: null,
    items: [
      { href: "/dashboard", label: "Dashboard", icon: "layout-dashboard", status: "ready" },
      { href: "/market-health", label: "Market Health", icon: "activity", status: "ready" },
      { href: "/screens", label: "Screens", icon: "table-2", status: "ready" },
      {
        href: "/portfolios",
        label: "Rebalance Tracker",
        icon: "scale",
        status: "planned",
        arrivesIn: "Prompt 14",
      },
      {
        href: "/backtests",
        label: "Backtests",
        icon: "history",
        status: "planned",
        arrivesIn: "Prompt 15",
      },
      { href: "/listings", label: "Listings", icon: "list", status: "ready" },
    ],
  },
  {
    label: "Account",
    items: [
      { href: "/pricing", label: "Pricing", icon: "tag", status: "planned", arrivesIn: "Prompt 13" },
      { href: "/invoices", label: "Invoices", icon: "receipt", status: "planned", arrivesIn: "Prompt 13" },
      { href: "/profile", label: "Profile", icon: "user", status: "ready" },
      { href: "/change-password", label: "Change Password", icon: "key-round", status: "ready" },
    ],
  },
  {
    label: "Help",
    items: [
      { href: "/faq", label: "FAQ", icon: "circle-help", status: "planned", arrivesIn: "Prompt 18" },
      { href: "/blog", label: "Blog", icon: "newspaper", status: "planned", arrivesIn: "Prompt 18" },
      { href: "/support", label: "Support", icon: "life-buoy", status: "planned", arrivesIn: "Prompt 18" },
    ],
  },
] as const;

/** Every nav destination, flattened — used by tests and by the command palette. */
export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((group) => group.items);
