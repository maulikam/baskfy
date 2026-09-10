import { describe, expect, it } from "vitest";

import {
  allocationAnalytics,
  percentOf,
  spreadFor,
  type AllocationAnalytics,
} from "@/lib/portfolio/analytics";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import type { Unallocated } from "@/lib/portfolio/organize";

/**
 * The dashboard's numbers, asserted against fixtures rather than against a screenshot.
 *
 * The property that matters most is the boring one: **weights add to 100**. A column that does not
 * is the kind of wrong a reader trusts, because every row looks reasonable on its own.
 */

function row(
  portfolio_id: number,
  name: string,
  value: string | null,
  extra: Partial<PortfolioRow> = {},
): PortfolioRow {
  return {
    portfolio_id,
    name,
    kind: "CAPITAL",
    source: "HOLDING_GROUP",
    source_badge: "Grouped",
    started_on: "2026-01-01",
    value: value ?? undefined,
    cash: "0",
    counts_toward_total: true,
    status: "Synced",
    holdings_count: 1,
    broker_count: 1,
    todays_pnl: { amount: "0", label: "flat" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2026-01-01",
      is_model: false,
    },
    ...extra,
  } as PortfolioRow;
}

function pile(holdings_value: string): Unallocated {
  return {
    cash: "0",
    cta: "Sort it",
    holdings_count: 1,
    holdings_value,
    pending_reconciliation: false,
    total_value: holdings_value,
  };
}

describe("percentOf", () => {
  it("is exact to two decimals", () => {
    expect(percentOf("25", "100")).toBe("25.00");
    expect(percentOf("1", "3")).toBe("33.33");
    expect(percentOf("2", "3")).toBe("66.67");
  });

  it("answers null rather than guessing", () => {
    expect(percentOf(null, "100")).toBeNull();
    expect(percentOf("25", null)).toBeNull();
    expect(percentOf("25", "0")).toBeNull();
    expect(percentOf("25", "not a number")).toBeNull();
  });

  it("handles a negative part, because a day can be down", () => {
    expect(percentOf("-25", "100")).toBe("-25.00");
  });
});

describe("spreadFor", () => {
  it("names the shape of the book at its own thresholds", () => {
    expect(spreadFor("80")).toBe("concentrated");
    expect(spreadFor("50")).toBe("balanced"); // the boundary is "above", not "at"
    expect(spreadFor("30")).toBe("balanced");
    expect(spreadFor("25")).toBe("spread");
    expect(spreadFor(null)).toBeNull();
  });
});

describe("allocationAnalytics", () => {
  const rows = [
    row(1, "Long term", "40000"),
    row(2, "Swing", "30000"),
    row(3, "Momentum", "20000"),
  ];

  function sumOfWeights(analytics: AllocationAnalytics): number {
    return analytics.slices.reduce((total, slice) => total + Number(slice.weightPct ?? 0), 0);
  }

  it("weights every portfolio against the whole book, unallocated included", () => {
    const analytics = allocationAnalytics(rows, pile("10000"));

    expect(analytics.totalValue).toBe("100000");
    expect(analytics.allocatedValue).toBe("90000");
    expect(analytics.unallocatedPct).toBe("10.00");
    expect(analytics.slices.map((slice) => [slice.name, slice.weightPct])).toEqual([
      ["Long term", "40.00"],
      ["Swing", "30.00"],
      ["Momentum", "20.00"],
    ]);
  });

  it("the weights and the unallocated share add to 100", () => {
    const analytics = allocationAnalytics(rows, pile("10000"));

    expect(sumOfWeights(analytics) + Number(analytics.unallocatedPct)).toBeCloseTo(100, 10);
  });

  it("orders by value, biggest first", () => {
    const analytics = allocationAnalytics(
      [row(1, "Small", "1"), row(2, "Big", "999")],
      pile("0"),
    );

    expect(analytics.slices.map((slice) => slice.name)).toEqual(["Big", "Small"]);
    expect(analytics.topShare).toBe("99.90");
    expect(analytics.spread).toBe("concentrated");
  });

  it("leaves an unpriced portfolio out of the arithmetic and says how many", () => {
    /* Counting it as zero would shrink the denominator, inflate everyone else's weight, and still
       total 100 — the failure that looks most like success. */
    const analytics = allocationAnalytics(
      [row(1, "Priced", "50000"), row(2, "No close on record", null)],
      pile("50000"),
    );

    expect(analytics.unpricedCount).toBe(1);
    expect(analytics.totalValue).toBe("100000");
    expect(analytics.slices[0]?.weightPct).toBe("50.00");
    expect(analytics.slices[1]?.weightPct).toBeNull();
  });

  it("puts an unpriced portfolio last, never first", () => {
    const analytics = allocationAnalytics([row(1, "Unpriced", null), row(2, "Priced", "5")], null);

    expect(analytics.slices.map((slice) => slice.name)).toEqual(["Priced", "Unpriced"]);
  });

  it("adds the top three exactly, not by adding their rounded weights", () => {
    const analytics = allocationAnalytics(
      [row(1, "A", "1"), row(2, "B", "1"), row(3, "C", "1"), row(4, "D", "1")],
      pile("0"),
    );

    // Each is 25.00, and the three together are 75.00 — computed from the rupees, so a ratio that
    // did not divide evenly could not accumulate three roundings.
    expect(analytics.topThreeShare).toBe("75.00");
  });

  it("names today's best and worst, and adds the day exactly", () => {
    const analytics = allocationAnalytics(
      [
        row(1, "Up", "100", { todays_pnl: { amount: "500", label: "up" } }),
        row(2, "Down", "100", { todays_pnl: { amount: "-200", label: "down" } }),
        row(3, "Flat", "100", { todays_pnl: { amount: "0", label: "flat" } }),
      ],
      null,
    );

    expect(analytics.bestToday?.name).toBe("Up");
    expect(analytics.worstToday?.name).toBe("Down");
    expect(analytics.todaysTotal).toBe("300");
  });

  it("names no best or worst when there is only one portfolio to name", () => {
    /* "Your best performer today is your only portfolio" is a statement about a book of one. */
    const analytics = allocationAnalytics(
      [row(1, "Only", "100", { todays_pnl: { amount: "5", label: "up" } })],
      null,
    );

    expect(analytics.bestToday).toBeNull();
    expect(analytics.worstToday).toBeNull();
  });

  it("survives an empty book without inventing a figure", () => {
    const analytics = allocationAnalytics([], null);

    expect(analytics.totalValue).toBeNull();
    expect(analytics.topShare).toBeNull();
    expect(analytics.spread).toBeNull();
    expect(analytics.portfolioCount).toBe(0);
    expect(analytics.holdingsCount).toBe(0);
  });

  it("counts holdings across every portfolio", () => {
    const analytics = allocationAnalytics(
      [row(1, "A", "1", { holdings_count: 12 }), row(2, "B", "1", { holdings_count: 3 })],
      null,
    );

    expect(analytics.holdingsCount).toBe(15);
  });
});
