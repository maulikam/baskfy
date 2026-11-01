import { describe, expect, it } from "vitest";

import { ladderPositions, linearScale, payoffDomains, payoffGeometry } from "@/lib/opportunities/payoff";

const spread = {
  id: "bcs",
  breakevens: [2510],
  points: [
    { underlying: 2400, pnl_per_lot: -5000 },
    { underlying: 2500, pnl_per_lot: -5000 },
    { underlying: 2600, pnl_per_lot: 7500 },
    { underlying: 2700, pnl_per_lot: 7500 },
  ],
};

describe("linearScale", () => {
  it("maps domain ends to range ends, including an inverted range", () => {
    const y = linearScale([-100, 100], [200, 0]);
    expect(y(-100)).toBe(200);
    expect(y(100)).toBe(0);
    expect(y(0)).toBe(100);
  });

  it("puts a degenerate domain in the middle of the range", () => {
    expect(linearScale([5, 5], [0, 10])(5)).toBe(5);
  });
});

describe("payoffDomains", () => {
  it("spans every payoff point and pads P&L so the lines never touch the frame", () => {
    const d = payoffDomains([spread], null);
    expect(d?.xDomain).toEqual([2400, 2700]);
    expect(d?.yDomain[0]).toBeLessThan(-5000);
    expect(d?.yDomain[1]).toBeGreaterThan(7500);
  });

  it("always includes zero, the spot and every breakeven", () => {
    const allProfit = { id: "p", breakevens: [2350], points: [{ underlying: 2400, pnl_per_lot: 100 }, { underlying: 2500, pnl_per_lot: 300 }] };
    const d = payoffDomains([allProfit], 2800);
    expect(d?.xDomain).toEqual([2350, 2800]);
    expect(d?.yDomain[0]).toBeLessThan(0);
  });

  it("returns null when nothing is selected", () => {
    expect(payoffDomains([], 2500)).toBeNull();
  });
});

describe("payoffGeometry", () => {
  it("draws loss below the zero line and profit above it inside the margins", () => {
    const g = payoffGeometry([spread], 2550, 400, 200, { top: 10, right: 10, bottom: 20, left: 40 });
    expect(g).not.toBeNull();
    if (!g) return;
    expect(g.x(2400)).toBe(40);
    expect(g.x(2700)).toBe(390);
    expect(g.y(-5000)).toBeGreaterThan(g.y(0));
    expect(g.y(7500)).toBeLessThan(g.y(0));
    expect(g.y(g.yDomain[0])).toBe(180);
    expect(g.y(g.yDomain[1])).toBe(10);
  });
});

describe("ladderPositions", () => {
  it("places invalidation at 0% and the furthest target at 100%", () => {
    const p = ladderPositions(90, 100, [110, 130]);
    expect(p.invalidation).toBe(0);
    expect(p.entry).toBeCloseTo(25);
    expect(p.targets).toEqual([50, 100]);
  });
});
