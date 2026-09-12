import { describe, expect, it } from "vitest";

import {
  DEFAULT_SCREEN_ID,
  type SymbolSet,
  intersectionOf,
  intersectionSummary,
  pickScreenId,
} from "@/lib/overlap/overlap";

function set(key: string, symbols: readonly string[] | null): SymbolSet {
  return { key, label: key, symbols };
}

describe("intersection across Build scans", () => {
  it("keeps only the names every source named", () => {
    const result = intersectionOf([
      set("Volume breakout", ["RELIANCE", "TCS", "INFY"]),
      set("Three weeks tight", ["TCS", "INFY", "HDFCBANK"]),
    ]);
    expect(result.shared).toEqual(["INFY", "TCS"]);
    expect(result.sharedCount).toBe(2);
    expect(result.available).toBe(true);
  });

  it("intersects three sources the same way", () => {
    const result = intersectionOf([
      set("Screens", ["RELIANCE", "TCS", "INFY", "WIPRO"]),
      set("Volume breakout", ["TCS", "INFY", "HDFCBANK"]),
      set("Three weeks tight", ["INFY", "TCS", "SBIN"]),
    ]);
    expect(result.shared).toEqual(["INFY", "TCS"]);
    expect(result.summary).toMatch(/share 2 stocks/);
  });

  it("intersects Swing and Three weeks tight the same way", () => {
    const result = intersectionOf([
      set("Swing", ["RELIANCE", "TCS", "INFY"]),
      set("Three weeks tight", ["TCS", "INFY", "HDFCBANK"]),
    ]);
    expect(result.shared).toEqual(["INFY", "TCS"]);
    expect(result.sharedCount).toBe(2);
    expect(result.available).toBe(true);
    expect(result.summary).toMatch(/Swing and Three weeks tight/);
  });

  it("does not invent a zero when a source could not be read", () => {
    const result = intersectionOf([
      set("Volume breakout", ["TCS"]),
      set("Three weeks tight", null),
    ]);
    expect(result.available).toBe(false);
    expect(result.sharedCount).toBe(0);
    expect(result.summary).toMatch(/could not be read/);
    expect(result.summary).toMatch(/not the same as "no overlap"/);
  });

  it("says when a source genuinely named nothing", () => {
    const result = intersectionOf([
      set("Volume breakout", []),
      set("Three weeks tight", ["TCS"]),
    ]);
    expect(result.available).toBe(true);
    expect(result.sharedCount).toBe(0);
    expect(result.summary).toMatch(/named no stocks/);
  });

  it("normalises case and strips blanks", () => {
    const result = intersectionOf([
      set("a", [" tcs ", "INFY"]),
      set("b", ["TCS", "infy", ""]),
    ]);
    expect(result.shared).toEqual(["INFY", "TCS"]);
  });
});

describe("intersection summary wording", () => {
  it("names every source when nothing is shared", () => {
    expect(intersectionSummary(["Volume breakout", "Three weeks tight"], 0, [3, 4])).toMatch(
      /share no stocks/,
    );
  });

  it("joins three labels with an and before the last", () => {
    expect(intersectionSummary(["Screens", "Volume breakout", "Three weeks tight"], 2, [10, 5, 8])).toBe(
      "Screens, Volume breakout, and Three weeks tight share 2 stocks.",
    );
  });
});

describe("which screen the page runs", () => {
  const screens = [
    { publicId: "exmpl0000001", name: "Investing 001" },
    { publicId: "user00000001", name: "My screen" },
  ];

  it("uses the query when it names a real screen", () => {
    expect(pickScreenId("user00000001", screens)).toBe("user00000001");
  });

  it("falls back to the seeded example when the query is missing or unknown", () => {
    expect(pickScreenId(undefined, screens)).toBe(DEFAULT_SCREEN_ID);
    expect(pickScreenId("nope", screens)).toBe(DEFAULT_SCREEN_ID);
  });
});
