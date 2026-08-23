import { describe, expect, it } from "vitest";

import { NAV_GROUPS, NAV_ITEMS } from "@/lib/nav";
import { PAGES } from "@/lib/vocabulary";

/**
 * docs/08 §"App shell" states the sidebar's contents verbatim:
 *
 *     "Dashboard · Market Health · Screens · Rebalance Tracker · Backtests · Listings — then
 *      Account (Pricing, Invoices, Profile, Change Password) — then Help (FAQ, Blog, Support)."
 *
 * ## What M36 changed, and what it did not
 *
 * Until M36 this file pinned that sentence **as words**. It now pins it **as routes**, and the
 * words are asserted to come from `lib/vocabulary` instead.
 *
 * That is a change of spec, not a loosened test. Maulik's instruction of 23 Aug 2026 was to make
 * the product readable by somebody who does not already have the vocabulary, and every one of
 * "Market Health", "Rebalance Tracker", "Market stance" and "Plan vs fills" requires you to know
 * the term before you can decide whether to click it. The order docs/08 records is an observation
 * about the reference product's information architecture and is still pinned exactly; the labels
 * were a naming choice, and the naming choice changed (`docs/DECISIONS-MERGE.md` §M36.1).
 *
 * The test got *stronger* in one respect: pinning routes rather than strings means a future
 * rename cannot silently point an item somewhere else, which the old string assertions allowed.
 *
 * **Explore, Investments, Watchlist, and Baskets are deliberate additions to docs/08's list.**
 * docs/08 predates the merge and the curated-basket catalog. SC5 adds `/explore` (public catalog).
 * SC6 adds `/investments` and `/watchlist` (investor surfaces); `/fees` lands in Account.
 * M22's `/baskets` remains the desk MomentumScan operator view; they soft-coexist (DECISIONS-SC
 * SC5). Explore → Investments → Watchlist sit before Baskets so discovery and holdings come first.
 *
 * **The "Real money" group is M26's addition, renamed by M36.** Five of the desk console's own
 * pages moved onto the web app, and they are grouped rather than scattered through the primary
 * list because they answer a different kind of question: the primary group analyses a *market*,
 * this group reports a *portfolio* that is actually being traded. It sits before Account for the
 * same reason — it is product, not settings. Every page in it is read-only; execution stays in
 * the desk console (MERGE-PROMPTS.md §M26).
 */
describe("the sidebar IA", () => {
  it("has docs/08's groups in order, with M26's own group between product and settings", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([null, "Real money", "Account", "Help"]);
  });

  it("lists the primary group in docs/08's order, by route", () => {
    expect(NAV_GROUPS[0]?.items.map((item) => item.href)).toEqual([
      "/dashboard",
      "/market-health",
      "/screens",
      "/portfolios",
      "/explore",
      "/investments",
      "/watchlist",
      "/baskets",
      "/backtests",
      "/listings",
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

  it("lists the account group with fees and M41's brokers page", () => {
    // docs/08 named Pricing, Invoices, Profile, Change Password. SC6 Fees sits beside
    // Invoices (accrued ledger). Brokers is the P5.8 surface next to Profile.
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
    // A disabled nav item has to explain itself; "coming soon" with no date is noise.
    for (const item of NAV_ITEMS.filter((entry) => entry.status === "planned")) {
      expect(item.arrivesIn, `${item.label} has no arrival`).toMatch(/^Prompt \d+$/);
    }
  });
});

/**
 * M36's actual claim: the sidebar, the page heading and the browser tab say the same words,
 * because there is only one copy of them.
 */
describe("the sidebar's words", () => {
  it("takes every label and blurb from the one vocabulary record", () => {
    for (const item of NAV_ITEMS) {
      const entry = PAGES[item.href as keyof typeof PAGES];
      expect(entry, `${item.href} has no vocabulary entry`).toBeDefined();
      expect(item.label).toBe(entry.title);
      expect(item.blurb).toBe(entry.blurb);
    }
  });

  it("keeps the professional name for every label that replaced one", () => {
    // The renamed items are the point of M36 — each must still be able to say what it used to be
    // called, or an experienced user cannot find the page they already know.
    const renamed = ["/market-health", "/portfolios", "/regime", "/reconcile", "/backtests"];
    for (const href of renamed) {
      const item = NAV_ITEMS.find((entry) => entry.href === href);
      expect(item?.formerly, `${href} was renamed without keeping its old name`).toBeTruthy();
    }
  });

  it("gives every item a blurb short enough to read in a tooltip", () => {
    for (const item of NAV_ITEMS) {
      expect(item.blurb.length, `${item.label}'s blurb is an essay`).toBeLessThanOrEqual(96);
    }
  });
});
