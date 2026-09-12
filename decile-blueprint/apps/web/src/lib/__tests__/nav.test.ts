import { describe, expect, it } from "vitest";

import {
  LEGACY_REDIRECTS,
  NAV_GROUPS,
  NAV_ITEMS,
  PRIMARY_NAV,
  primarySection,
  SECTION_TABS,
} from "@/lib/nav";
import { PAGES } from "@/lib/vocabulary";

/**
 * Tree 6 collapsed the consumer IA to Market · Baskets · Build · Me; SC9 added the signed-in
 * landing surface in front of them; PORTFOLIO_REDESIGN.md §2 replaced Me with Portfolio and gave
 * it five tabs of its own. Desk Real money + Account + Help remain secondary groups.
 *
 * Five, not four, and the count is asserted rather than inferred: a sixth destination is an IA
 * decision, and it should have to change a test that says so out loud.
 */
describe("the primary consumer IA", () => {
  it("exposes exactly five primary destinations, home first", () => {
    expect(PRIMARY_NAV.map((item) => item.label)).toEqual([
      "Home",
      "Market",
      "Discover",
      "Build",
      "Portfolio",
    ]);
    expect(PRIMARY_NAV).toHaveLength(5);
  });

  /*
    PORTFOLIO_REDESIGN.md §2, asserted by name rather than by shape. "Me" was the fifth
    destination and it held the money *and* the account; the money is its own hub now and the
    landing tab is Overview, so the pill points there rather than at a bare `/portfolio`.
  */
  it("makes the money its own destination, landing on Overview", () => {
    const portfolio = PRIMARY_NAV.find((item) => item.label === "Portfolio");
    expect(portfolio?.href).toBe("/portfolio/overview");
    expect(PRIMARY_NAV.map((item) => item.label)).not.toContain("Me");
    expect(SECTION_TABS.portfolio[0]?.label).toBe("Overview");
  });

  it("puts the landing surface at /home, not at the screener's market dashboard", () => {
    expect(PRIMARY_NAV[0]?.href).toBe("/home");
    expect(LEGACY_REDIRECTS.find((r) => r.source === "/dashboard")?.destination).toBe(
      "/market/today",
    );
  });

  it("marks /home as its own section so the pill and the tab bar light up", () => {
    expect(primarySection("/home")).toBe("home");
    expect(primarySection("/market/today")).toBe("market");
  });

  it("keeps primary labels one word and short enough not to truncate", () => {
    for (const item of PRIMARY_NAV) {
      expect(item.label.includes(" "), `${item.label} is not one word`).toBe(false);
      expect(item.label.length, `${item.label} is long`).toBeLessThanOrEqual(10);
    }
  });

  it("lists primary routes in the first nav group", () => {
    expect(NAV_GROUPS[0]?.items.map((item) => item.href)).toEqual([
      "/home",
      "/market/today",
      "/discover",
      "/build",
      "/portfolio/overview",
    ]);
  });

  it("gives home no section tabs — its modules are one page, not a hub", () => {
    expect(SECTION_TABS.home).toEqual([]);
  });

  it("keeps section tabs for each hub", () => {
    expect(SECTION_TABS.market.map((t) => t.label)).toEqual(["Today", "Mood", "Listings"]);
    /* Discover's five tabs. "Create" is deliberately gone: it builds a strategy, which is Build's
       job, and a second entrance from inside Discover made the two sections look like rivals. */
    expect(SECTION_TABS.discover.map((t) => t.label)).toEqual([
      "For you",
      "All baskets",
      "Collections",
      "Compare",
      "Saved",
    ]);
    expect(SECTION_TABS.discover.map((t) => t.href)).not.toContain("/create");
    // Create moved here out of Discover: it builds a strategy, which is this hub's job.
    /* SW4 added Swing: `docs/swing/05` §1 puts the swing hub inside Build rather than adding a
       sixth primary destination, because HOME1 fixes the primary chrome at five. */
    /* VB8 added the third: `docs/vbt/05` §1 puts the volume-breakout hub inside Build for the
       same reason the swing hub is there — HOME1 fixes the primary chrome at five. */
    /* TW8 added the fourth, for the same reason again (`docs/twt/05` §1). The list is asserted
       in full rather than by `toContain` deliberately: a sixth strategy tab inside Build is an
       IA decision and it should have to change a test that says so out loud. */
    expect(SECTION_TABS.build.map((t) => t.label)).toEqual([
      "Screens",
      "Backtests",
      "Create",
      "Swing",
      "Volume breakout",
      "Three weeks tight",
    ]);
    /* `docs/vbt/05` §2's "Today | Book | Backtest". The middle tab reads **Positions**, matching
       the swing hub's and PORTFOLIO_REDESIGN.md §8's retirement of "book" from reader-facing
       copy; the route keeps its documented path (DECISIONS-VB VB8.5). */
    expect(SECTION_TABS.vbt.map((t) => t.label)).toEqual([
      "Today",
      "Positions",
      "Backtest",
    ]);
    expect(SECTION_TABS.vbt.map((t) => t.href)).toEqual([
      "/vbt",
      "/vbt/book",
      "/vbt/backtest",
    ]);
    /* `docs/twt/05` §1's hub is two tabs: the open positions sit on the hub itself, so there is
       no third. TW8, DECISIONS-TW TW8.6. */
    expect(SECTION_TABS.twt.map((t) => t.label)).toEqual(["Today", "Backtest"]);
    expect(SECTION_TABS.twt.map((t) => t.href)).toEqual(["/twt", "/twt/backtest"]);
    for (const tab of SECTION_TABS.twt) {
      expect(PAGES[tab.href as keyof typeof PAGES], `${tab.href} has no page record`).toBeDefined();
    }
    expect(SECTION_TABS.swing.map((t) => t.label)).toEqual([
      "Setups",
      "Watchlist",
      "Market",
      "Positions",
      "Journal",
    ]);
    expect(SECTION_TABS.swing.map((t) => t.href)).toContain("/swing/journal");
    expect(SECTION_TABS.build.map((t) => t.href)).toContain("/create");
    /*
      §2: `Me → Investments | Portfolios | Watchlist` becomes
      `Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist`. Overview leads
      because it is the default landing tab; "Investments" is gone as a word, because it and
      "Portfolios" were two names for one thing (§1 problem 1).
    */
    expect(SECTION_TABS.portfolio.map((t) => t.label)).toEqual([
      "Overview",
      "Portfolios",
      "Holdings",
      "Activity",
      "Watchlist",
    ]);
    expect(SECTION_TABS.portfolio.map((t) => t.href)).toEqual([
      "/portfolio/overview",
      "/portfolio/portfolios",
      "/portfolio/holdings",
      "/portfolio/activity",
      "/portfolio/watchlist",
    ]);
    expect(SECTION_TABS.portfolio.map((t) => t.label)).not.toContain("Investments");

    /* §2's other half: Me keeps profile / settings / subscription and no money.
       The "Security" tab went with `/change-password` when Google sign-in replaced the password
       (`docs/DECISIONS-MERGE.md` M46) — there is no credential of ours left to manage, so the tab
       had nowhere to point. What it used to say is now a sentence on `/profile` telling the
       reader their sign-in lives in their Google account. */
    /* SW14: `docs/swing/05` §2 puts the swing settings "inside `/me`, not a hub tab". */
    expect(SECTION_TABS.me.map((t) => t.label)).toEqual([
      "Profile",
      "Brokers",
      "Subscription",
      "Swing settings",
    ]);
    for (const tab of SECTION_TABS.me) {
      expect(tab.href.startsWith("/portfolio"), `${tab.href} is money, not account`).toBe(false);
    }
  });

  /*
    Every money path that used to sit under Me now lights Portfolio, including on the way through
    the redirect. `/portfolios` (plural) is the desk's rebalance sub-tree and must not be caught
    by a sloppy `startsWith("/portfolio")`.
  */
  it("routes the old Me money paths to the Portfolio section", () => {
    expect(primarySection("/portfolio/overview")).toBe("portfolio");
    expect(primarySection("/portfolio/holdings")).toBe("portfolio");
    expect(primarySection("/me/investments")).toBe("portfolio");
    expect(primarySection("/me/portfolios")).toBe("portfolio");
    expect(primarySection("/me/watchlist")).toBe("portfolio");
    expect(primarySection("/investments")).toBe("portfolio");
    expect(primarySection("/portfolios/12/rebalance")).toBe("portfolio");
    expect(primarySection("/me")).toBe("me");
  });

  /*
    A tab that navigates nowhere is worse than a tab that is missing, so the five §2 destinations
    are checked against the route table the app actually publishes.
  */
  it("gives every Portfolio tab a page in the vocabulary route table", () => {
    for (const tab of SECTION_TABS.portfolio) {
      expect(PAGES[tab.href as keyof typeof PAGES], `${tab.href} has no page record`).toBeDefined();
    }
  });

  it("documents every legacy permanent redirect, so no old path silently 404s", () => {
    expect(LEGACY_REDIRECTS).toHaveLength(23);
  });

  it("keeps every `/baskets*` path resolving after the rename to Discover", () => {
    const sources = LEGACY_REDIRECTS.map((entry) => entry.source);
    for (const path of [
      "/baskets",
      "/baskets/featured",
      "/baskets/plan",
      "/baskets/collections",
    ]) {
      expect(sources, `${path} still resolves`).toContain(path);
    }
    const hub = LEGACY_REDIRECTS.find((entry) => entry.source === "/baskets");
    expect(hub?.destination).toBe("/discover");
  });

  it("keeps every path the money section used to live at resolving", () => {
    const map = new Map(LEGACY_REDIRECTS.map((entry) => [entry.source, entry.destination]));
    expect(map.get("/me/investments")).toBe("/portfolio/overview");
    expect(map.get("/me/portfolios")).toBe("/portfolio/portfolios");
    expect(map.get("/me/watchlist")).toBe("/portfolio/watchlist");
    // The pre-Tree-6 flat paths are re-pointed at the new homes, not chained through `/me/*`.
    expect(map.get("/investments")).toBe("/portfolio/overview");
    expect(map.get("/portfolios")).toBe("/portfolio/portfolios");
    expect(map.get("/watchlist")).toBe("/portfolio/watchlist");
    for (const destination of map.values()) {
      expect(destination.startsWith("/me/"), `${destination} still lands under Me`).toBe(false);
    }
  });

  it("lets a reader find Discover by the name it used to have", () => {
    // A rename must not orphan the people who learned the previous word; the ⌘K palette
    // searches `formerly` for exactly this reason.
    const discover = PRIMARY_NAV.find((item) => item.label === "Discover");
    expect(discover?.formerly).toBe("Baskets");
  });
});

describe("the sidebar IA", () => {
  it("has primary, Real money, Account, Help in order", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([
      null,
      "Real money",
      "Account",
      "Help",
    ]);
  });

  it("lists the desk's five read-only routes as M26 delivers them", () => {
    expect(NAV_GROUPS[1]?.items.map((item) => item.href)).toEqual([
      "/performance",
      "/holdings",
      "/tradebook",
      "/regime",
      "/reconcile",
    ]);
  });

  it("lists the account group with fees and brokers", () => {
    expect(NAV_GROUPS[2]?.items.map((item) => item.href)).toEqual([
      "/pricing",
      "/invoices",
      "/fees",
      "/profile",
      "/brokers",
      // SW14: the swing settings, under Me (`docs/swing/05` §2).
      "/me/swing",
    ]);
  });

  it("lists the help group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[3]?.items.map((item) => item.href)).toEqual(["/faq", "/blog", "/support"]);
  });

  it("has no duplicate destinations", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("has no planned destinations left", () => {
    expect(NAV_ITEMS.every((entry) => entry.status === "ready")).toBe(true);
  });
});

describe("the sidebar's words", () => {
  it("takes every non-primary label and blurb from the vocabulary record", () => {
    for (const item of NAV_ITEMS) {
      if (PRIMARY_NAV.some((p) => p.href === item.href)) continue;
      const entry = PAGES[item.href as keyof typeof PAGES];
      expect(entry, `${item.href} has no vocabulary entry`).toBeDefined();
      expect(item.label).toBe(entry.title);
      expect(item.blurb).toBe(entry.blurb);
    }
  });

  it("keeps the professional name for renamed Portfolio/Market pages", () => {
    expect(PAGES["/portfolio/portfolios"].formerly).toBe("Rebalance Tracker");
    // "Investments" is retired as a tab but stays searchable in ⌘K, exactly as "Baskets" did.
    expect(PAGES["/portfolio/overview"].formerly).toBe("Investments");
    expect(PAGES["/market/mood"].formerly).toBe("Market Health");
  });

  it("gives every item a blurb short enough to read in a tooltip", () => {
    for (const item of NAV_ITEMS) {
      expect(item.blurb.length, `${item.label}'s blurb is an essay`).toBeLessThanOrEqual(96);
    }
  });
});
