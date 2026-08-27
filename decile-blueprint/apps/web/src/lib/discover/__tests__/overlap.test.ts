import { describe, expect, it } from "vitest";

import {
  type HoldingSet,
  overlapMatrix,
  overlapOf,
  overlapSummary,
  strongestOverlap,
} from "@/lib/discover/overlap";

function set(slug: string, symbols: readonly string[] | null): HoldingSet {
  return { slug, name: slug.replace(/-/g, " "), symbols };
}

describe("overlap between two baskets", () => {
  it("counts the stocks they actually share", () => {
    const result = overlapOf(
      set("alpha", ["RELIANCE", "TCS", "INFY"]),
      set("beta", ["TCS", "INFY", "HDFCBANK"]),
    );
    expect(result.shared).toEqual(["INFY", "TCS"]);
    expect(result.sharedCount).toBe(2);
    expect(result.countA).toBe(3);
    expect(result.countB).toBe(3);
    expect(result.jaccard).toBeCloseTo(2 / 4);
    expect(result.available).toBe(true);
  });

  it("is case- and whitespace-insensitive, and never counts a duplicate twice", () => {
    const result = overlapOf(
      set("alpha", [" reliance ", "RELIANCE", "TCS"]),
      set("beta", ["Reliance"]),
    );
    expect(result.countA).toBe(2);
    expect(result.sharedCount).toBe(1);
  });

  it("says both denominators, because 14 of 25 and 14 of 15 are different facts", () => {
    const result = overlapOf(
      set("wide", Array.from({ length: 25 }, (_, i) => `S${i}`)),
      set("narrow", Array.from({ length: 15 }, (_, i) => `S${i}`)),
    );
    expect(result.summary).toMatch(/share 15 stocks/);
    expect(result.summary).toMatch(/15 of wide's 25/);
    expect(result.summary).toMatch(/15 of narrow's 15/);
  });

  it("calls out two baskets that are the same basket", () => {
    const result = overlapOf(set("alpha", ["A", "B"]), set("beta", ["B", "A"]));
    expect(result.jaccard).toBe(1);
    expect(result.summary).toMatch(/exactly the same 2 stocks/);
    expect(result.summary).toMatch(/concentrates rather than diversifies/);
  });

  it("says plainly when there is no overlap at all", () => {
    const result = overlapOf(set("alpha", ["A"]), set("beta", ["B"]));
    expect(result.sharedCount).toBe(0);
    expect(result.summary).toMatch(/share no holdings/);
  });
});

describe("unreadable is not zero", () => {
  it("refuses to report 0% when holdings could not be read", () => {
    // A comparison that turns "we do not know" into "no overlap" invents a reassuring number,
    // which is the one thing a comparison must never do.
    const result = overlapOf(set("alpha", ["A", "B"]), set("beta", null));
    expect(result.available).toBe(false);
    expect(result.summary).toMatch(/could not be read/);
    expect(result.summary).toMatch(/not the same as "no overlap"/);
  });

  it("names every basket whose holdings were unreadable", () => {
    const result = overlapOf(set("alpha", null), set("beta", null));
    expect(result.summary).toMatch(/alpha and beta/);
  });

  it("distinguishes a basket that holds nothing from one that could not be read", () => {
    const empty = overlapOf(set("alpha", []), set("beta", ["A"]));
    expect(empty.available).toBe(true);
    expect(empty.summary).toMatch(/no holdings recorded/);
    expect(empty.summary).not.toMatch(/could not be read/);
  });
});

describe("more than two baskets", () => {
  const three = [
    set("a", ["A", "B", "C"]),
    set("b", ["B", "C", "D"]),
    set("c", ["X", "Y", "Z"]),
  ];

  it("produces every pair once, in selection order", () => {
    const pairs = overlapMatrix(three);
    expect(pairs.map((pair) => `${pair.a.slug}-${pair.b.slug}`)).toEqual(["a-b", "a-c", "b-c"]);
  });

  it("produces one pair for two baskets and none for one", () => {
    expect(overlapMatrix(three.slice(0, 2))).toHaveLength(1);
    expect(overlapMatrix(three.slice(0, 1))).toHaveLength(0);
    expect(overlapMatrix([])).toHaveLength(0);
  });

  it("leads with the pair that overlaps most", () => {
    const strongest = strongestOverlap(overlapMatrix(three));
    expect(strongest?.a.slug).toBe("a");
    expect(strongest?.b.slug).toBe("b");
  });

  it("returns nothing to lead with when no pair shares anything", () => {
    expect(strongestOverlap(overlapMatrix([set("a", ["A"]), set("b", ["B"])]))).toBeNull();
  });

  it("ignores unreadable pairs when choosing what to lead with", () => {
    const pairs = overlapMatrix([set("a", null), set("b", ["B", "C"]), set("c", ["B", "C"])]);
    expect(strongestOverlap(pairs)?.a.slug).toBe("b");
  });
});

describe("the summary wording", () => {
  it("is built in one place, so the table and the headline cannot disagree", () => {
    expect(overlapSummary("One", "Two", 3, 10, 4)).toBe(
      "One and Two share 3 stocks — 3 of One's 10 and 3 of Two's 4.",
    );
  });
});
