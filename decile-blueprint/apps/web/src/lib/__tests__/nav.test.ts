import { describe, expect, it } from "vitest";

import {
  LEGACY_REDIRECTS,
  NAV_GROUPS,
  NAV_ITEMS,
  PRIMARY_NAV,
  SECTION_TABS,
} from "@/lib/nav";
import { PAGES } from "@/lib/vocabulary";

/**
 * Tree 6: consumer IA collapses to Market · Baskets · Build · Me.
 * Desk Real money + Account + Help remain secondary groups.
 */
describe("the primary consumer IA", () => {
  it("exposes exactly four primary destinations", () => {
    expect(PRIMARY_NAV.map((item) => item.label)).toEqual([
      "Market",
      "Baskets",
      "Build",
      "Me",
    ]);
    expect(PRIMARY_NAV).toHaveLength(4);
  });

  it("keeps primary labels one word and short enough not to truncate", () => {
    for (const item of PRIMARY_NAV) {
      expect(item.label.includes(" "), `${item.label} is not one word`).toBe(false);
      expect(item.label.length, `${item.label} is long`).toBeLessThanOrEqual(10);
    }
  });

  it("lists primary routes in the first nav group", () => {
    expect(NAV_GROUPS[0]?.items.map((item) => item.href)).toEqual([
      "/market/today",
      "/baskets",
      "/build",
      "/me/investments",
    ]);
  });

  it("keeps section tabs for each hub", () => {
    expect(SECTION_TABS.market.map((t) => t.label)).toEqual(["Today", "Mood", "Listings"]);
    expect(SECTION_TABS.baskets.map((t) => t.label)).toEqual(["Explore", "Featured"]);
    expect(SECTION_TABS.build.map((t) => t.label)).toEqual(["Screens", "Backtests"]);
    expect(SECTION_TABS.me.map((t) => t.label)).toEqual([
      "Investments",
      "Portfolios",
      "Watchlist",
    ]);
  });

  it("documents thirteen legacy permanent redirects for Tree 6", () => {
    expect(LEGACY_REDIRECTS).toHaveLength(13);
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
      "/change-password",
    ]);
  });

  it("lists the help group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[3]?.items.map((item) => item.href)).toEqual(["/faq", "/blog", "/support"]);
  });

  it("has no duplicate destinations", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("gives every not-yet-built destination a prompt to point at", () => {
    for (const item of NAV_ITEMS.filter((entry) => entry.status === "planned")) {
      expect(item.arrivesIn, `${item.label} has no arrival`).toMatch(/^Prompt \d+$/);
    }
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

  it("keeps the professional name for renamed Me/Market pages", () => {
    expect(PAGES["/me/portfolios"].formerly).toBe("Rebalance Tracker");
    expect(PAGES["/market/mood"].formerly).toBe("Market Health");
  });

  it("gives every item a blurb short enough to read in a tooltip", () => {
    for (const item of NAV_ITEMS) {
      expect(item.blurb.length, `${item.label}'s blurb is an essay`).toBeLessThanOrEqual(96);
    }
  });
});
