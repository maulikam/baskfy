import { describe, expect, it } from "vitest";

import { materializeBasket } from "@/lib/basket/materialize";

describe("materializeBasket", () => {
  const rows = [
    { symbol: "AAA", name: "Aaa Ltd", rank: 1, close_raw: 100 },
    { symbol: "BBB", name: "Bbb Ltd", rank: 2, close_raw: 200 },
    { symbol: "CCC", name: "Ccc Ltd", rank: 3, close_raw: 50 },
  ];

  it("equal-weights top N with a 5% cash buffer by default", () => {
    const basket = materializeBasket({
      name: "Test",
      rows,
      topN: 2,
      notional: 100_000,
    });
    expect(basket.holdings).toHaveLength(2);
    expect(basket.cashPct).toBe(5);
    const weights = basket.holdings.map((h) => h.weight);
    expect(weights[0]).toBeCloseTo(0.475, 5);
    expect(weights[1]).toBeCloseTo(0.475, 5);
    expect(weights[0]! + weights[1]! + 0.05).toBeCloseTo(1, 5);
  });

  it("sizes amounts to the notional", () => {
    const basket = materializeBasket({
      name: "Test",
      rows,
      topN: 2,
      notional: 100_000,
    });
    const total = basket.holdings.reduce((s, h) => s + h.amount, 0);
    expect(total).toBe(95_000);
  });

  it("deploys everything when the investor opts out of a cash sleeve", () => {
    const basket = materializeBasket({
      name: "Test",
      rows,
      topN: 3,
      notional: 100_000,
      cashPct: 0,
    });
    expect(basket.cashPct).toBe(0);
    expect(basket.deployed).toBeGreaterThan(99_000);
    expect(basket.cash).toBeLessThan(1_000);
  });
});
