/**
 * Consumer information architecture (Tree 6 / baskfynavrefactorreport).
 *
 * Primary chrome is five one-word destinations: Home · Market · Baskets · Build · Me.
 *
 * Tree 6 collapsed it to four and deliberately had no landing surface; SC9 added one, and a
 * `/home` reachable only through the wordmark is a page most people never find. Five is what the
 * observed product's bottom bar carries too, and it still fits the 768px tab bar. The argument
 * and how to reverse it are `docs/DECISIONS-MERGE.md` HOME1.
 * Section tabs inside each hub carry the old ten flat links. Desk "Real money" pages and
 * Account/Help stay in the user menu — they are operator surfaces, not the Gen Z consumer IA.
 *
 * Legacy routes permanently redirect via `next.config.ts` and thin `redirect()` pages.
 */
import type { Route } from "next";

import { PAGES, type PagePath } from "@/lib/vocabulary";

export type NavStatus = "ready" | "planned";

interface NavItemBase {
  /** The plain-English name, from `lib/vocabulary` or a primary-nav override. */
  label: string;
  /** One sentence saying what the destination answers. */
  blurb: string;
  /** The professional name this label replaced, where it replaced one. */
  formerly?: string;
  /** lucide-react icon name, resolved in the shell so this module stays serialisable. */
  icon: NavIconName;
}

export interface ReadyNavItem extends NavItemBase {
  status: "ready";
  href: Route;
}

export interface PlannedNavItem extends NavItemBase {
  status: "planned";
  href: string;
  arrivesIn: string;
}

export type NavItem = ReadyNavItem | PlannedNavItem;

export interface NavGroup {
  label: string | null;
  items: NavItem[];
}

export type NavIconName =
  | "house"
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
  | "percent"
  | "line-chart"
  | "wrench"
  | "store";

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

/**
 * The four consumer destinations in the sticky header / mobile bottom bar.
 * Labels are fixed one-word IA tokens — not the section page titles (Today, Explore, …).
 */
export const PRIMARY_NAV: readonly ReadyNavItem[] = [
  {
    href: "/home",
    label: "Home",
    blurb: "What you hold, what needs a decision, and what is worth a look.",
    icon: "house",
    status: "ready",
  },
  {
    href: "/market/today",
    label: "Market",
    blurb: "Indices, mood, and new listings.",
    icon: "line-chart",
    status: "ready",
  },
  {
    href: "/baskets",
    label: "Baskets",
    blurb: "Browse curated baskets and today’s featured list.",
    icon: "store",
    status: "ready",
  },
  {
    href: "/build",
    label: "Build",
    blurb: "Screens, templates, and backtests.",
    icon: "wrench",
    status: "ready",
  },
  {
    href: "/me/investments",
    label: "Me",
    blurb: "Investments, portfolios, and watchlist.",
    icon: "user",
    status: "ready",
  },
] as const;

/** Section tabs rendered inside each hub page header — not in the global nav. */
export const SECTION_TABS = {
  /* Home is a single surface, not a hub: its modules are sections of one page, so there is
     nothing for a tab row to switch between. The key exists so `primarySection` can name it. */
  home: [],
  market: [
    { href: "/market/today" as Route, label: "Today" },
    { href: "/market/mood" as Route, label: "Mood" },
    { href: "/market/listings" as Route, label: "Listings" },
  ],
  baskets: [
    { href: "/baskets" as Route, label: "Explore" },
    { href: "/baskets/featured" as Route, label: "Featured" },
    { href: "/create" as Route, label: "Create" },
  ],
  build: [
    { href: "/build" as Route, label: "Screens" },
    { href: "/build/backtests" as Route, label: "Backtests" },
  ],
  me: [
    { href: "/me/investments" as Route, label: "Investments" },
    { href: "/me/portfolios" as Route, label: "Portfolios" },
    { href: "/me/watchlist" as Route, label: "Watchlist" },
  ],
} as const;

/**
 * Permanent redirects from Tree 6 — mirrored in `next.config.ts` `redirects()`.
 * E2e and vitest import this list so the route table cannot drift silently.
 */
export const LEGACY_REDIRECTS = [
  { source: "/dashboard", destination: "/market/today" },
  { source: "/market-health", destination: "/market/mood" },
  { source: "/listings", destination: "/market/listings" },
  { source: "/explore", destination: "/baskets" },
  { source: "/screens", destination: "/build" },
  { source: "/screens/new", destination: "/build/new" },
  { source: "/screens/exmpl0000001", destination: "/build/exmpl0000001" },
  { source: "/screens/exmpl0000001/columns", destination: "/build/exmpl0000001/columns" },
  { source: "/backtests", destination: "/build/backtests" },
  { source: "/backtests/000000000001", destination: "/build/backtests/000000000001" },
  { source: "/investments", destination: "/me/investments" },
  { source: "/investments/foo/bar", destination: "/me/investments/foo/bar" },
  { source: "/portfolios", destination: "/me/portfolios" },
  { source: "/watchlist", destination: "/me/watchlist" },
] as const;

export type SectionKey = keyof typeof SECTION_TABS;

/** Which primary nav item is active for a pathname. */
export function primarySection(pathname: string): SectionKey | null {
  if (pathname === "/home" || pathname.startsWith("/home/")) return "home";
  if (pathname === "/market" || pathname.startsWith("/market/")) return "market";
  if (
    pathname === "/baskets" ||
    pathname.startsWith("/baskets/") ||
    pathname.startsWith("/basket/") ||
    pathname === "/create"
  )
    return "baskets";
  if (pathname === "/build" || pathname.startsWith("/build/")) return "build";
  if (pathname === "/me" || pathname.startsWith("/me/")) return "me";
  // Legacy paths still mark the right pill while redirects settle.
  if (
    pathname.startsWith("/dashboard") ||
    pathname.startsWith("/market-health") ||
    pathname.startsWith("/listings")
  )
    return "market";
  if (pathname.startsWith("/explore")) return "baskets";
  if (pathname.startsWith("/screens") || pathname.startsWith("/backtests")) return "build";
  if (
    pathname.startsWith("/investments") ||
    pathname.startsWith("/portfolios") ||
    pathname.startsWith("/watchlist")
  )
    return "me";
  return null;
}

/**
 * Full nav groups: primary four first, then Real money (desk read-only), Account, Help.
 * User menu still draws Account/Help (+ Real money); TopNav draws PRIMARY_NAV only.
 */
export const NAV_GROUPS: readonly NavGroup[] = [
  {
    label: null,
    items: [...PRIMARY_NAV],
  },
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
