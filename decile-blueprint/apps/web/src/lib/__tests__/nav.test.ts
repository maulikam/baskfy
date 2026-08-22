import { describe, expect, it } from "vitest";

import { NAV_GROUPS, NAV_ITEMS } from "@/lib/nav";

/**
 * docs/08 §"App shell" states the sidebar's contents verbatim:
 *
 *     "Dashboard · Market Health · Screens · Rebalance Tracker · Backtests · Listings — then
 *      Account (Pricing, Invoices, Profile, Change Password) — then Help (FAQ, Blog, Support)."
 *
 * The order is the reference product's information architecture, not a preference, so it is pinned
 * rather than described. A reordering that is deliberate updates this list; one that is accidental
 * fails here.
 *
 * **Baskets is the one addition, and it is deliberate.** docs/08 predates the merge: it describes
 * the screener alone, which had no basket to show. MERGE-PROMPTS.md §M22 adds the desk's output as
 * a read-only surface, and it sits directly after Rebalance Tracker because that is the item it
 * belongs beside — what the strategy wants, next to what was actually traded.
 *
 * **The Desk group is M26's addition, and also deliberate.** Five of the desk console's own pages
 * moved onto the web app, and they are grouped rather than scattered through the primary list
 * because they answer a different kind of question: the primary group analyses a *market*, the
 * Desk group reports a *portfolio* that is actually being traded. It sits before Account for the
 * same reason — it is product, not settings. Every page in it is read-only; execution stays in the
 * desk console (MERGE-PROMPTS.md §M26).
 */
describe("the sidebar IA", () => {
  it("has docs/08's groups in order, with M26's Desk group between product and settings", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([null, "Desk", "Account", "Help"]);
  });

  it("lists the primary group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[0]?.items.map((item) => item.label)).toEqual([
      "Dashboard",
      "Market Health",
      "Screens",
      "Rebalance Tracker",
      "Baskets",
      "Backtests",
      "Listings",
    ]);
  });

  it("lists the desk group as M26 delivers it", () => {
    expect(NAV_GROUPS[1]?.items.map((item) => item.label)).toEqual([
      "Performance",
      "Holdings",
      "Trades",
      "Market stance",
      "Plan vs fills",
    ]);
  });

  it("lists the account group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[2]?.items.map((item) => item.label)).toEqual([
      "Pricing",
      "Invoices",
      "Profile",
      "Change Password",
    ]);
  });

  it("lists the help group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[3]?.items.map((item) => item.label)).toEqual(["FAQ", "Blog", "Support"]);
  });

  it("uses the routes docs/08 §Routes and docs/01 §1 name", () => {
    const byLabel = new Map(NAV_ITEMS.map((item) => [item.label, item.href]));
    expect(byLabel.get("Dashboard")).toBe("/dashboard");
    expect(byLabel.get("Market Health")).toBe("/market-health");
    expect(byLabel.get("Screens")).toBe("/screens");
    expect(byLabel.get("Listings")).toBe("/listings");
  });

  it("points the desk group at the M26 routes", () => {
    const byLabel = new Map(NAV_ITEMS.map((item) => [item.label, item.href]));
    expect(byLabel.get("Performance")).toBe("/performance");
    expect(byLabel.get("Holdings")).toBe("/holdings");
    expect(byLabel.get("Trades")).toBe("/tradebook");
    expect(byLabel.get("Market stance")).toBe("/regime");
    expect(byLabel.get("Plan vs fills")).toBe("/reconcile");
  });

  it("gives every not-yet-built destination a prompt to point at", () => {
    // A disabled nav item has to explain itself; "coming soon" with no date is noise.
    for (const item of NAV_ITEMS.filter((entry) => entry.status === "planned")) {
      expect(item.arrivesIn, `${item.label} has no arrival`).toMatch(/^Prompt \d+$/);
    }
  });

  it("has no duplicate destinations", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });
});
