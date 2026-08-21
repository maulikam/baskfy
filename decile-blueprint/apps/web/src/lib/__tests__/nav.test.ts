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
 */
describe("the sidebar IA", () => {
  it("has the three groups docs/08 names, in order", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([null, "Account", "Help"]);
  });

  it("lists the primary group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[0]?.items.map((item) => item.label)).toEqual([
      "Dashboard",
      "Market Health",
      "Screens",
      "Rebalance Tracker",
      "Backtests",
      "Listings",
    ]);
  });

  it("lists the account group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[1]?.items.map((item) => item.label)).toEqual([
      "Pricing",
      "Invoices",
      "Profile",
      "Change Password",
    ]);
  });

  it("lists the help group exactly as docs/08 does", () => {
    expect(NAV_GROUPS[2]?.items.map((item) => item.label)).toEqual(["FAQ", "Blog", "Support"]);
  });

  it("uses the routes docs/08 §Routes and docs/01 §1 name", () => {
    const byLabel = new Map(NAV_ITEMS.map((item) => [item.label, item.href]));
    expect(byLabel.get("Dashboard")).toBe("/dashboard");
    expect(byLabel.get("Market Health")).toBe("/market-health");
    expect(byLabel.get("Screens")).toBe("/screens");
    expect(byLabel.get("Listings")).toBe("/listings");
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
