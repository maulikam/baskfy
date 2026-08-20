import { describe, expect, it } from "vitest";

import { groupByFamily, matchesQuery } from "@/components/data/factor-combobox";
import { DEMO_FACTORS } from "@/app/(app)/kitchen-sink/fixtures";

/**
 * docs/08 §"Screen editor": the sort-by control is "a searchable combobox grouped by family
 * (Absolute / Sharpe / RSI / Beta-adjusted / Skip-month / Other)".
 *
 * The grouping and the search are pure functions so they can be asserted directly; the popover
 * around them is Radix's, and testing Radix is not this suite's job.
 */
describe("grouping", () => {
  it("uses docs/08's families, in docs/08's order", () => {
    expect(groupByFamily(DEMO_FACTORS).map((group) => group.label)).toEqual([
      "Absolute return",
      "Sharpe return",
      "RSI",
      "Beta-adjusted",
      "Skip-month",
      "Other",
    ]);
  });

  it("omits a family with no members rather than showing an empty heading", () => {
    const onlyRsi = DEMO_FACTORS.filter((factor) => factor.family === "rsi");
    expect(groupByFamily(onlyRsi).map((group) => group.label)).toEqual(["RSI"]);
  });

  it("keeps a family the registry adds later, at the end", () => {
    const groups = groupByFamily([
      ...DEMO_FACTORS,
      { key: "x", label: "X", family: "brand_new", unit: "ratio", higher_is_better: true },
    ]);
    expect(groups[groups.length - 1]?.family).toBe("brand_new");
  });

  it("loses no factor", () => {
    const total = groupByFamily(DEMO_FACTORS).reduce(
      (sum, group) => sum + group.factors.length,
      0,
    );
    expect(total).toBe(DEMO_FACTORS.length);
  });
});

describe("search", () => {
  const sharpe = DEMO_FACTORS.find((factor) => factor.key === "avg_sharpe_12_6_3_1");

  it("matches the label a user can see", () => {
    expect(matchesQuery(sharpe!, "average sharpe")).toBe(true);
  });

  it("matches the key a power user knows", () => {
    expect(matchesQuery(sharpe!, "avg_sharpe_12")).toBe(true);
  });

  it("is case-insensitive", () => {
    expect(matchesQuery(sharpe!, "SHARPE")).toBe(true);
  });

  it("matches everything when the query is empty", () => {
    expect(DEMO_FACTORS.every((factor) => matchesQuery(factor, ""))).toBe(true);
  });

  it("rejects a query that appears nowhere", () => {
    expect(matchesQuery(sharpe!, "momentum")).toBe(false);
  });
});
