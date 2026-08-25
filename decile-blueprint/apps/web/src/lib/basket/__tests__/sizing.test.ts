/**
 * SB1 — the sizing the screen itself cannot answer: how many names, and how much money.
 *
 * The profile suggests and the investor decides, so most of what is asserted here is that an
 * explicit count wins. The arithmetic is spec'd in Python
 * (`packages/core/tests/test_basket_sizing.py`) and the two tables are tied together by
 * `test_holding_profile_parity.py`; these tests cover what only the preview does — clamping a
 * half-typed number instead of erroring, and keeping the rupees reconciled while a slider moves.
 */

import { describe, expect, it } from "vitest";

import { materializeBasket } from "@/lib/basket/materialize";
import {
  DEFAULT_METHOD,
  fillMissingCustomWeights,
  schemeWeights,
  seedEqualPercents,
} from "@/lib/basket/methods";
import {
  cashPctForTier,
  DEFAULT_PROFILE,
  MAX_HOLDINGS,
  MIN_CASH_BUFFER_PCT,
  MIN_HOLDINGS,
  resolveHoldings,
  resolveCashPct,
  ZERO_CASH_PCT,
  SUGGESTED_HOLDINGS,
  suggestedHoldings,
} from "@/lib/basket/profiles";

function rows(count: number, price = 500) {
  return Array.from({ length: count }, (_, i) => ({
    symbol: `SYM${i + 1}`,
    name: `Sym ${i + 1}`,
    rank: i + 1,
    close_raw: price,
  }));
}

describe("holding profiles", () => {
  it("makes the concentrated profile the smallest one", () => {
    expect(SUGGESTED_HOLDINGS.AGGRESSIVE).toBeLessThan(SUGGESTED_HOLDINGS.BALANCED);
    expect(SUGGESTED_HOLDINGS.BALANCED).toBeLessThan(SUGGESTED_HOLDINGS.CONSERVATIVE);
  });

  it("defaults to the count a screen has always previewed at", () => {
    expect(suggestedHoldings()).toBe(20);
    expect(DEFAULT_PROFILE).toBe("BALANCED");
  });
});

describe("resolveHoldings", () => {
  it("uses the profile's suggestion when the investor gave no number", () => {
    expect(resolveHoldings({ available: 100, profile: "AGGRESSIVE", requested: null })).toBe(12);
  });

  it("trims a suggestion to what the screen actually found", () => {
    expect(resolveHoldings({ available: 8, profile: "CONSERVATIVE", requested: null })).toBe(8);
  });

  it("lets an explicit count beat the suggestion", () => {
    expect(resolveHoldings({ available: 100, profile: "CONSERVATIVE", requested: 5 })).toBe(5);
  });

  it("clamps rather than throws, because this runs mid-keystroke", () => {
    // The save endpoint refuses a count it cannot fill. A preview that threw while somebody was
    // still typing "1" on the way to "15" would be useless, so it shows the closest basket.
    expect(resolveHoldings({ available: 30, profile: "BALANCED", requested: 1 })).toBe(MIN_HOLDINGS);
    expect(resolveHoldings({ available: 60, profile: "BALANCED", requested: 999 })).toBe(
      MAX_HOLDINGS,
    );
    expect(resolveHoldings({ available: 10, profile: "BALANCED", requested: 40 })).toBe(10);
    expect(resolveHoldings({ available: 30, profile: "BALANCED", requested: Number.NaN })).toBe(20);
  });
});

describe("cashPctForTier", () => {
  it("reads the desk's equity cap rather than inventing a second opinion", () => {
    expect(cashPctForTier("R2")).toBe(30);
    expect(cashPctForTier("R3")).toBe(60);
  });

  it("keeps the rounding floor even at full exposure", () => {
    // R1 caps equity at 100%, but whole-rupee equal weights always leave a remainder, so a
    // basket advertising 0% cash would be a promise the arithmetic cannot keep.
    expect(cashPctForTier("R1")).toBe(MIN_CASH_BUFFER_PCT);
  });

  it("treats an unknown or absent tier as no reason to withhold money", () => {
    expect(cashPctForTier(null)).toBe(MIN_CASH_BUFFER_PCT);
    expect(cashPctForTier("R9")).toBe(MIN_CASH_BUFFER_PCT);
  });

  it("never produces an all-cash basket", () => {
    // R4 is the desk's stand-aside tier. Refusing to deploy is a decision for the person, so the
    // preview still shows a real basket with very little in it.
    expect(cashPctForTier("R4")).toBeLessThanOrEqual(95);
    expect(cashPctForTier("R4")).toBe(95);
  });
});

describe("resolveCashPct", () => {
  it("uses the tier suggestion until the investor overrides it", () => {
    expect(resolveCashPct({ exposureTier: "R3", requested: null })).toBe(60);
    expect(resolveCashPct({ requested: null })).toBe(MIN_CASH_BUFFER_PCT);
  });

  it("honours an explicit opt-out of the cash sleeve", () => {
    expect(resolveCashPct({ exposureTier: "R3", requested: ZERO_CASH_PCT })).toBe(0);
  });
});

describe("materializeBasket sizing", () => {
  it("takes its name count from the profile", () => {
    const basket = materializeBasket({ name: "T", rows: rows(40), profile: "AGGRESSIVE" });
    expect(basket.holdings).toHaveLength(12);
    expect(basket.profile).toBe("AGGRESSIVE");
  });

  it("reports no profile once the investor has chosen a count", () => {
    const basket = materializeBasket({
      name: "T",
      rows: rows(40),
      profile: "AGGRESSIVE",
      holdings: 7,
    });
    expect(basket.holdings).toHaveLength(7);
    expect(basket.profile).toBeNull();
  });

  it("reconciles to the rupee at any amount", () => {
    for (const notional of [10_000, 33_333, 100_000, 250_001, 1_000_000]) {
      const basket = materializeBasket({ name: "T", rows: rows(30), notional, holdings: 7 });
      const deployed = basket.holdings.reduce((sum, h) => sum + h.amount, 0);
      expect(deployed).toBe(basket.deployed);
      expect(basket.deployed + basket.cash).toBe(notional);
    }
  });

  it("gives every name the same rupees, remainder to cash", () => {
    // Equal weight has to mean equal money in the table the investor reads. A weight-derived
    // split would hand the last name the rounding residual and look like a bigger position.
    const basket = materializeBasket({ name: "T", rows: rows(3), notional: 100_000, holdings: 3 });
    const amounts = new Set(basket.holdings.map((h) => h.amount));
    expect(amounts.size).toBe(1);
    expect(basket.cash).toBeGreaterThanOrEqual(5_000);
  });

  it("holds more cash in a defensive tier without changing the count", () => {
    const full = materializeBasket({ name: "T", rows: rows(30), exposureTier: "R1" });
    const defensive = materializeBasket({ name: "T", rows: rows(30), exposureTier: "R3" });
    expect(defensive.cash).toBeGreaterThan(full.cash);
    expect(defensive.holdings).toHaveLength(full.holdings.length);
  });

  it("flags an amount too small to fill every name", () => {
    const basket = materializeBasket({
      name: "T",
      rows: rows(20, 4_000),
      notional: 5_000,
      holdings: 20,
    });
    expect(basket.underfunded).toBe(true);
    expect(basket.minInvestment).toBeGreaterThan(5_000);
  });
});

describe("weight methods", () => {
  it("defaults to equal so existing baskets do not move", () => {
    expect(DEFAULT_METHOD).toBe("EQUAL");
    const implied = materializeBasket({ name: "T", rows: rows(5), holdings: 5 });
    const equal = materializeBasket({
      name: "T",
      rows: rows(5),
      holdings: 5,
      method: "EQUAL",
    });
    expect(implied.holdings.map((h) => h.amount)).toEqual(equal.holdings.map((h) => h.amount));
    expect(implied.method).toBe("EQUAL");
  });

  it("gives the best name the largest slice under rank", () => {
    const basket = materializeBasket({
      name: "T",
      rows: rows(4),
      holdings: 4,
      method: "RANK",
      notional: 100_000,
    });
    const amounts = basket.holdings.map((h) => h.amount);
    expect(amounts).toEqual([...amounts].sort((a, b) => b - a));
    expect(basket.holdings[0]?.symbol).toBe("SYM1");
    expect(basket.deployed + basket.cash).toBe(100_000);
  });

  it("follows the screen factor under score, not the rank", () => {
    const basket = materializeBasket({
      name: "T",
      rows: [
        { symbol: "LOW", rank: 1, sorting_factor: 1, close_raw: 100 },
        { symbol: "HIGH", rank: 2, sorting_factor: 9, close_raw: 100 },
      ],
      holdings: 2,
      method: "SCORE",
      notional: 100_000,
    });
    const bySymbol = Object.fromEntries(basket.holdings.map((h) => [h.symbol, h.amount]));
    expect(bySymbol.HIGH).toBeGreaterThan(bySymbol.LOW as number);
  });

  it("gives the calmer name more under inverse-vol", () => {
    const basket = materializeBasket({
      name: "T",
      rows: [
        { symbol: "JUMPY", rank: 1, vol: 0.5, close_raw: 100 },
        { symbol: "CALM", rank: 2, vol: 0.1, close_raw: 100 },
      ],
      holdings: 2,
      method: "INV_VOL",
      notional: 100_000,
    });
    const bySymbol = Object.fromEntries(basket.holdings.map((h) => [h.symbol, h.amount]));
    expect(bySymbol.CALM).toBeGreaterThan(bySymbol.JUMPY as number);
  });

  it("applies the investor's numbers under custom", () => {
    const basket = materializeBasket({
      name: "T",
      rows: rows(3),
      holdings: 3,
      method: "CUSTOM",
      customWeights: { SYM1: 70, SYM2: 20, SYM3: 10 },
      notional: 100_000,
    });
    expect(basket.holdings[0]?.weight).toBeCloseTo(0.665, 3);
    expect(basket.holdings[0]?.amount).toBeGreaterThan(basket.holdings[2]?.amount ?? 0);
    expect(basket.deployed + basket.cash).toBe(100_000);
  });

  it("falls back to equal when custom is empty, because this runs mid-keystroke", () => {
    const empty = materializeBasket({
      name: "T",
      rows: rows(3),
      holdings: 3,
      method: "CUSTOM",
      customWeights: {},
    });
    const equal = materializeBasket({ name: "T", rows: rows(3), holdings: 3, method: "EQUAL" });
    expect(empty.holdings.map((h) => h.amount)).toEqual(equal.holdings.map((h) => h.amount));
  });

  it("uses rank-inside-the-set, not absolute rank", () => {
    const weights = schemeWeights(
      [
        { symbol: "A", rank: 10 },
        { symbol: "B", rank: 11 },
        { symbol: "C", rank: 12 },
      ],
      "RANK",
    );
    expect(weights[0]).toBeCloseTo(0.5, 4);
    expect(weights[1]).toBeCloseTo(0.3333, 4);
    expect(weights[2]).toBeCloseTo(0.1667, 4);
  });

  it("seeds equal percents and fills only the names the investor has not typed", () => {
    expect(seedEqualPercents(["A", "B"])).toEqual({ A: 50, B: 50 });
    expect(fillMissingCustomWeights(["A", "B", "C"], { A: 70 })).toEqual({
      A: 70,
      B: expect.any(Number) as number,
      C: expect.any(Number) as number,
    });
    expect(fillMissingCustomWeights(["A"], { A: 0 }).A).toBe(0);
  });
});
