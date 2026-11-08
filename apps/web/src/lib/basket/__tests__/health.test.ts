import { describe, expect, it } from "vitest";

import {
  assessBasketHealth,
  FEW_NAMES,
  healthRowsFrom,
  median,
  numericField,
  SINGLE_NAME_CAP_PCT,
} from "@/lib/basket/health";

describe("median", () => {
  it("is null on an empty list, so a missing column does not become a number", () => {
    expect(median([])).toBeNull();
  });

  it("returns the middle value of an odd list and the mean of the two middles of an even one", () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 2, 3])).toBe(2.5);
  });
});

describe("numericField", () => {
  it("reads a number, a numeric string, or nothing", () => {
    expect(numericField({ vol_12m: 0.28 }, "vol_12m")).toBe(0.28);
    expect(numericField({ vol_12m: "0.28" }, "vol_12m")).toBe(0.28);
    expect(numericField({}, "vol_12m")).toBeNull();
    expect(numericField({ vol_12m: "—" }, "vol_12m")).toBeNull();
  });
});

describe("assessBasketHealth", () => {
  it("reports the desk's single-name cap when one name is more than 15% of the sleeve", () => {
    const health = assessBasketHealth([
      { symbol: "AAA", weightOfAmount: 0.2 },
      { symbol: "BBB", weightOfAmount: 0.2 },
    ]);
    expect(SINGLE_NAME_CAP_PCT).toBe(15);
    expect(health.largestSleevePct).toBe(50);
    expect(health.notes.map((note) => note.kind)).toContain("single-name");
    expect(health.notes.some((note) => note.text.includes("15%"))).toBe(true);
  });

  it("does not raise the cap note when equal weight stays inside it", () => {
    const weight = 0.95 / 20;
    const health = assessBasketHealth(
      Array.from({ length: 20 }, (_, i) => ({
        symbol: `S${i}`,
        weightOfAmount: weight,
      })),
    );
    expect(health.largestSleevePct).toBeCloseTo(5, 5);
    expect(health.notes.map((note) => note.kind)).not.toContain("single-name");
  });

  it("notes a short list without calling it a problem to fix", () => {
    const health = assessBasketHealth([
      { symbol: "AAA", weightOfAmount: 0.4 },
      { symbol: "BBB", weightOfAmount: 0.4 },
    ]);
    expect(health.names).toBeLessThan(FEW_NAMES);
    expect(health.notes.map((note) => note.kind)).toContain("few-names");
    expect(health.notes.find((note) => note.kind === "few-names")?.text).not.toMatch(
      /should|diversify|recommend/i,
    );
  });

  it("takes the median return and vol of the names that have them", () => {
    const health = assessBasketHealth([
      { symbol: "A", weightOfAmount: 0.3, ret12m: 0.1, vol12m: 0.2 },
      { symbol: "B", weightOfAmount: 0.3, ret12m: 0.3, vol12m: 0.4 },
      { symbol: "C", weightOfAmount: 0.3, ret12m: null, vol12m: 0.3 },
    ]);
    expect(health.medianRet12m).toBe(0.2);
    expect(health.medianVol12m).toBe(0.3);
  });

  it("joins sized holdings to the screen row's 1-year columns", () => {
    const rows = healthRowsFrom(
      [
        { symbol: "aaa", weight: 0.4 },
        { symbol: "BBB", weight: 0.4 },
      ],
      [
        { symbol: "AAA", ret_12m: 12, vol_12m: 0.2 },
        { symbol: "BBB", ret_12m: 8, vol_12m: 0.3 },
      ],
    );
    expect(rows[0]).toEqual({
      symbol: "aaa",
      weightOfAmount: 0.4,
      ret12m: 12,
      vol12m: 0.2,
    });
    expect(assessBasketHealth(rows).medianRet12m).toBe(10);
  });

  it("is empty, not zero, when there are no names", () => {
    const health = assessBasketHealth([]);
    expect(health.names).toBe(0);
    expect(health.largestSleevePct).toBeNull();
    expect(health.medianRet12m).toBeNull();
    expect(health.notes).toEqual([]);
  });
});
