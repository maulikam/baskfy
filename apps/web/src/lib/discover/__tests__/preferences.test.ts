import { describe, expect, it } from "vitest";

import { DEFAULT_PREFERENCES, type Preferences } from "@/lib/discover/match";
import {
  MAX_AMOUNT,
  MIN_AMOUNT,
  hasStatedPreferences,
  preferencesFromParams,
  preferencesToQuery,
  validAmount,
} from "@/lib/discover/preferences";

const stated: Preferences = {
  goal: "high-growth",
  horizon: "1-3",
  risk: "higher",
  amount: 250_000,
  rebalance: "MONTHLY",
};

describe("preferences round-trip through the URL", () => {
  it("survives being serialised and read back", () => {
    const params = new URLSearchParams(preferencesToQuery(stated));
    expect(preferencesFromParams(params)).toEqual(stated);
  });

  it("reads a Next.js searchParams object as well as URLSearchParams", () => {
    expect(preferencesFromParams({ risk: "lower" }).risk).toBe("lower");
    expect(preferencesFromParams({ risk: ["lower", "higher"] }).risk).toBe("lower");
  });

  it("falls back to defaults for anything missing", () => {
    expect(preferencesFromParams({})).toEqual(DEFAULT_PREFERENCES);
  });
});

describe("a hand-edited URL cannot reach the matcher", () => {
  it("replaces an unknown value with the default rather than passing it through", () => {
    // A preference the matcher does not understand would stop being checked silently, which
    // quietly changes what "3 of 4" means.
    expect(preferencesFromParams({ risk: "extreme" }).risk).toBe(DEFAULT_PREFERENCES.risk);
    expect(preferencesFromParams({ goal: "get-rich" }).goal).toBe(DEFAULT_PREFERENCES.goal);
    expect(preferencesFromParams({ horizon: "forever" }).horizon).toBe(
      DEFAULT_PREFERENCES.horizon,
    );
    expect(preferencesFromParams({ rebalance: "HOURLY" }).rebalance).toBe(
      DEFAULT_PREFERENCES.rebalance,
    );
  });

  it("cannot be used to inject a prototype key", () => {
    expect(preferencesFromParams({ goal: "constructor" }).goal).toBe(DEFAULT_PREFERENCES.goal);
    expect(preferencesFromParams({ goal: "toString" }).goal).toBe(DEFAULT_PREFERENCES.goal);
  });

  it("rejects an amount outside the range a person could mean", () => {
    expect(validAmount(String(MIN_AMOUNT - 1))).toBe(DEFAULT_PREFERENCES.amount);
    expect(validAmount(String(MAX_AMOUNT + 1))).toBe(DEFAULT_PREFERENCES.amount);
    expect(validAmount("-5000")).toBe(DEFAULT_PREFERENCES.amount);
    expect(validAmount("not a number")).toBe(DEFAULT_PREFERENCES.amount);
    expect(validAmount(undefined)).toBe(DEFAULT_PREFERENCES.amount);
  });

  it("accepts an amount typed with separators, the way a person types it", () => {
    expect(validAmount("5,00,000")).toBe(500_000);
    expect(validAmount("₹250000")).toBe(250_000);
  });

  it("keeps an amount at the edges of the range", () => {
    expect(validAmount(MIN_AMOUNT)).toBe(MIN_AMOUNT);
    expect(validAmount(MAX_AMOUNT)).toBe(MAX_AMOUNT);
  });
});

describe("knowing whether the reader said anything", () => {
  it("is false on a bare visit, so the page does not claim a filter was applied", () => {
    expect(hasStatedPreferences({})).toBe(false);
    expect(hasStatedPreferences(new URLSearchParams())).toBe(false);
  });

  it("is true as soon as one preference is present", () => {
    expect(hasStatedPreferences({ risk: "lower" })).toBe(true);
    expect(hasStatedPreferences(new URLSearchParams(preferencesToQuery(stated)))).toBe(true);
  });

  it("ignores unrelated parameters", () => {
    expect(hasStatedPreferences({ b: "some-basket", view: "table" })).toBe(false);
  });
});
