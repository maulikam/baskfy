/**
 * Consumer information architecture (Tree 6 / baskfynavrefactorreport).
 *
 * Primary chrome is five one-word destinations: Home · Market · Discover · Build · Portfolio.
 *
 * Tree 6 collapsed it to four and deliberately had no landing surface; SC9 added one, and a
 * `/home` reachable only through the wordmark is a page most people never find. Five is what the
 * observed product's bottom bar carries too, and it still fits the 768px tab bar. The argument
 * and how to reverse it are `docs/DECISIONS-MERGE.md` HOME1.
 * Section tabs inside each hub carry the old ten flat links. Desk "Real money" pages and
 * Account/Help stay in the user menu — they are operator surfaces, not the Gen Z consumer IA.
 *
 * The fifth destination was "Me", and PORTFOLIO_REDESIGN.md §2 took it apart: the money became
 * its own hub (`Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist`) and Me kept
 * profile, brokers, subscription and security. `Investments` and `Portfolios` were two words for
 * one idea (§1 problem 1) and are one tab row now; the nesting/sleeve vocabulary §8 retires does
 * not appear in this table at all.
 *
 * Legacy routes permanently redirect via `next.config.ts` and thin `redirect()` pages.
 */
import type { Route } from "next";

import { PAGES, type PagePath } from "@/lib/vocabulary";

/**
 * Every nav destination is ready. The `planned` status and `arrivesIn` machinery were deleted
 * (AUDIT 4.10) — there were no planned items left, and the type only existed to keep dead UI.
 */
export interface NavItem {
  /** The plain-English name, from `lib/vocabulary` or a primary-nav override. */
  label: string;
  /** One sentence saying what the destination answers. */
  blurb: string;
  /** The professional name this label replaced, where it replaced one. */
  formerly?: string;
  /** lucide-react icon name, resolved in the shell so this module stays serialisable. */
  icon: NavIconName;
  status: "ready";
  href: Route;
}

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

function page(href: PagePath & Route, icon: NavIconName): NavItem {
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
 * The five consumer destinations in the sticky header / mobile bottom bar.
 * Labels are fixed one-word IA tokens — not the section page titles (Today, Explore, …).
 */
export const PRIMARY_NAV: readonly NavItem[] = [
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
    href: "/discover",
    /*
      "Baskets" named the product's taxonomy; "Discover" names what a reader came to do. The
      destination is the same hub — every `/baskets*` path redirects to `/discover*` — and the
      word change is the point: nobody's goal is to navigate a product's noun.
    */
    label: "Discover",
    /* Searchable in the ⌘K palette, so typing the word a reader already knows still lands here. */
    formerly: "Baskets",
    blurb: "Find a basket that matches how you want to invest, and compare the candidates.",
    icon: "compass",
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
    /*
      PORTFOLIO_REDESIGN.md §2. "Me" used to hold the money and the account both, and its first
      tab was "Investments" sitting next to a tab called "Portfolios" — two names for one thing
      (§1 problem 1). The money is its own destination now, and `Me` keeps profile, brokers,
      subscription and security only. Overview is the section's landing tab, so the pill points
      at it rather than at a bare `/portfolio` that only redirects.
    */
    href: "/portfolio/overview",
    label: "Portfolio",
    blurb: "Everything you hold, across baskets and brokers, in one picture.",
    icon: "briefcase",
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
  /*
    Discover's five tabs. `Create` is deliberately absent: it builds a strategy, which is Build's
    whole job, and a second entrance to it from inside Discover made the two sections look like
    rivals. It is reachable from Build and from a basket's own "clone" affordance.
  */
  discover: [
    { href: "/discover" as Route, label: "For you" },
    { href: "/discover/all" as Route, label: "All baskets" },
    { href: "/discover/collections" as Route, label: "Collections" },
    { href: "/discover/compare" as Route, label: "Compare" },
    { href: "/discover/saved" as Route, label: "Saved" },
  ],
  build: [
    { href: "/build" as Route, label: "Screens" },
    { href: "/build/backtests" as Route, label: "Backtests" },
    /* Create left Discover; this is where it lives. Removing it from one hub without giving it
       a home in the other would have hidden a real surface behind a direct link. */
    { href: "/create" as Route, label: "Create" },
    /* `docs/swing/05` §1: "The **Build** hub gains a section tab **Swing**". */
    { href: "/swing" as Route, label: "Swing" },
    /* `docs/vbt/05` §1, the same shape: the third allocation is a tab beside the second, not a
       new hub. It is read-only here — a volume-breakout line becomes an order in the desk
       console and nowhere else (`docs/vbt/02` Track C §4). */
    { href: "/vbt" as Route, label: "Volume breakout" },
    /* `docs/twt/05` §1: the fourth allocation, by the same argument as the third. Build is where
       the strategies live, and HOME1 fixes the primary chrome at five destinations. */
    { href: "/twt" as Route, label: "Three weeks tight" },
  ],
  /*
    The swing hub (SW4, `docs/swing/05` §2): "Setups | Watchlist | Market | Positions | Journal".
    All five tabs are in as of SW8; the row carried only the pages that existed while the rest
    were being built, rather than showing dead links.
  */
  swing: [
    { href: "/swing" as Route, label: "Setups" },
    { href: "/swing/watchlist" as Route, label: "Watchlist" },
    { href: "/swing/market" as Route, label: "Market" },
    { href: "/swing/positions" as Route, label: "Positions" },
    { href: "/swing/journal" as Route, label: "Journal" },
  ],
  /*
    The volume-breakout hub (VB8, `docs/vbt/05` §2): "Today | Book | Backtest". Three tabs, and
    no fourth — there is no settings tab here because the sleeve's four numbers live under Me
    with the swing book's, for the same reason: they are about a person's money, not today's tape.
  */
  vbt: [
    { href: "/vbt" as Route, label: "Today" },
    /* `docs/vbt/05` §2 calls this tab "Book". The label is **Positions**, matching the swing
       hub's: PORTFOLIO_REDESIGN.md §8 retires "book" from what a reader sees, and two sleeves
       that show the same thing should not name it two different ways. The route keeps its
       documented `/vbt/book` path. DECISIONS-VB VB8.5. */
    { href: "/vbt/book" as Route, label: "Positions" },
    { href: "/vbt/backtest" as Route, label: "Backtest" },
  ],
  /*
    The three-weeks-tight hub (TW8, `docs/twt/05` §1 and §3): "Today | Backtest". Two tabs, not
    three — `05` §1 puts the open positions on the hub itself rather than on a page of their own,
    because this strategy holds about ten lines for about a year each and a second page to hold
    ten rows is a click in front of the thing the reader came for.
  */
  twt: [
    { href: "/twt" as Route, label: "Today" },
    { href: "/twt/backtest" as Route, label: "Backtest" },
  ],
  /*
    PORTFOLIO_REDESIGN.md §2, in order. Overview first because it is the default landing tab —
    the single consolidated screen (§6). Portfolios is the logical grouping layer, Holdings the
    flat broker-level truth beneath it, Activity the ledger behind both.
  */
  portfolio: [
    { href: "/portfolio/overview" as Route, label: "Overview" },
    { href: "/portfolio/portfolios" as Route, label: "Portfolios" },
    { href: "/portfolio/holdings" as Route, label: "Holdings" },
    { href: "/portfolio/activity" as Route, label: "Activity" },
    { href: "/portfolio/watchlist" as Route, label: "Watchlist" },
  ],
  /*
    What is left of Me: profile, settings, subscription, security. No investment data — that is
    the other half of §2, and it is why every `/me/*` money path now redirects into `/portfolio`.
    These four surfaces already exist and are drawn in the user menu (`NAV_GROUPS` "Account");
    the tab row is declared here so a Me hub page has one to render when it lands.
  */
  me: [
    { href: "/profile" as Route, label: "Profile" },
    { href: "/brokers" as Route, label: "Brokers" },
    { href: "/pricing" as Route, label: "Subscription" },
    /* `docs/swing/05` §2: the swing settings sit "inside `/me`, not a hub tab" (SW14). */
    { href: "/me/swing" as Route, label: "Swing settings" },
  ],
} as const;

/**
 * Which primary destination a section lights up.
 *
 * The header and the bottom bar each carried their own copy of this mapping, and both had
 * already drifted — the bar still said `discover: "Baskets"` after the hub was renamed, so the
 * Discover tab never lit. One record, read by both.
 */
export const SECTION_LABEL: Record<SectionKey, string> = {
  home: "Home",
  market: "Market",
  discover: "Discover",
  build: "Build",
  swing: "Build",
  vbt: "Build",
  twt: "Build",
  portfolio: "Portfolio",
  me: "Me",
};

/**
 * Permanent redirects from Tree 6 — mirrored in `next.config.ts` `redirects()`.
 * E2e and vitest import this list so the route table cannot drift silently.
 */
export const LEGACY_REDIRECTS = [
  { source: "/dashboard", destination: "/market/today" },
  { source: "/market-health", destination: "/market/mood" },
  { source: "/listings", destination: "/market/listings" },
  { source: "/explore", destination: "/discover" },
  { source: "/baskets", destination: "/discover" },
  { source: "/baskets/featured", destination: "/discover/featured" },
  { source: "/baskets/plan", destination: "/discover/plan" },
  { source: "/baskets/collections", destination: "/discover/collections" },
  { source: "/baskets/collections/momentum", destination: "/discover/collections/momentum" },
  { source: "/screens", destination: "/build" },
  { source: "/screens/new", destination: "/build/new" },
  { source: "/screens/exmpl0000001", destination: "/build/exmpl0000001" },
  { source: "/screens/exmpl0000001/columns", destination: "/build/exmpl0000001/columns" },
  { source: "/backtests", destination: "/build/backtests" },
  { source: "/backtests/000000000001", destination: "/build/backtests/000000000001" },
  /*
    PORTFOLIO_REDESIGN.md §2 moved the whole money section out of Me. The pre-existing
    `/investments`, `/portfolios` and `/watchlist` entries are re-pointed at the new homes rather
    than left chaining through `/me/*`: a two-hop redirect is a second thing to keep correct, and
    the first hop's destination no longer holds a page.
  */
  { source: "/investments", destination: "/portfolio/overview" },
  { source: "/investments/foo/bar", destination: "/portfolio/foo/bar" },
  { source: "/portfolios", destination: "/portfolio/portfolios" },
  { source: "/watchlist", destination: "/portfolio/watchlist" },
  { source: "/me/investments", destination: "/portfolio/overview" },
  { source: "/me/investments/foo/bar", destination: "/portfolio/foo/bar" },
  { source: "/me/portfolios", destination: "/portfolio/portfolios" },
  { source: "/me/watchlist", destination: "/portfolio/watchlist" },
] as const;

export type SectionKey = keyof typeof SECTION_TABS;

/** Which primary nav item is active for a pathname. */
export function primarySection(pathname: string): SectionKey | null {
  if (pathname === "/home" || pathname.startsWith("/home/")) return "home";
  if (pathname === "/market" || pathname.startsWith("/market/")) return "market";
  if (
    pathname === "/discover" ||
    pathname.startsWith("/discover/") ||
    // `/basket/[slug]` is singular and is a *detail* page, not the hub — it keeps its own path
    // and still lights the Discover pill, because that is the section a reader came in through.
    pathname.startsWith("/basket/") ||
    pathname === "/discover" ||
    pathname.startsWith("/baskets/")
  )
    return "discover";
  if (pathname === "/build" || pathname.startsWith("/build/") || pathname === "/create")
    return "build";
  // `docs/swing/05` §1: "The **Build** hub gains a section tab **Swing**". The swing pages light
  // Build in the primary chrome rather than adding a sixth destination — HOME1 fixes the primary
  // nav at five.
  if (pathname === "/swing" || pathname.startsWith("/swing/")) return "swing";
  // `docs/vbt/05` §1: the third sleeve, by the same argument. It lights Build too, and for the
  // same reason — the primary nav stays at five.
  if (pathname === "/vbt" || pathname.startsWith("/vbt/")) return "vbt";
  // `docs/twt/05` §1: the fourth sleeve, by the same argument again.
  if (pathname === "/twt" || pathname.startsWith("/twt/")) return "twt";
  /* `/portfolios` (plural, the desk's rebalance sub-pages) must not match here — hence the exact
     compare and the trailing slash, never a bare `startsWith("/portfolio")`. */
  if (pathname === "/portfolio" || pathname.startsWith("/portfolio/")) return "portfolio";
  /* The three money paths that used to live under Me light Portfolio, not Me — they redirect
     there, and a redirect that flashes the wrong pill on the way is still the wrong pill. */
  if (
    pathname.startsWith("/me/investments") ||
    pathname.startsWith("/me/portfolios") ||
    pathname.startsWith("/me/watchlist")
  )
    return "portfolio";
  if (pathname === "/me" || pathname.startsWith("/me/")) return "me";
  // Legacy paths still mark the right pill while redirects settle.
  if (
    pathname.startsWith("/dashboard") ||
    pathname.startsWith("/market-health") ||
    pathname.startsWith("/listings")
  )
    return "market";
  if (pathname.startsWith("/explore")) return "discover";
  if (pathname.startsWith("/screens") || pathname.startsWith("/backtests")) return "build";
  if (
    pathname.startsWith("/investments") ||
    pathname.startsWith("/portfolios") ||
    pathname.startsWith("/watchlist")
  )
    return "portfolio";
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
      /* SW14: the swing allocation's limits, under Me (`docs/swing/05` §2). */
      page("/me/swing", "wrench"),
    ],
  },
  {
    label: "Help",
    items: [page("/faq", "circle-help"), page("/blog", "newspaper"), page("/support", "life-buoy")],
  },
] as const;

/** Every nav destination, flattened — used by tests and by the command palette. */
export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flatMap((group) => group.items);
