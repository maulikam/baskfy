import { describe, expect, it } from "vitest";

import {
  DEFAULT_SCREEN_ID,
  type SymbolSet,
  filterMembershipRows,
  intersectionOf,
  intersectionSummary,
  membershipOf,
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

describe("membership across strategies and a screen", () => {
  it("tags each name with every source that named it, including the screen", () => {
    const result = membershipOf([
      set("vbt", ["RELIANCE", "TCS"]),
      set("twt", ["TCS", "INFY"]),
      set("swing", ["INFY", "HDFCBANK"]),
      { key: "exmpl0000001", label: "Investing 001", symbols: ["TCS", "WIPRO"] },
    ]);
    expect(result.available).toBe(true);
    expect(result.rows.map((row) => row.symbol)).toEqual([
      "TCS",
      "INFY",
      "HDFCBANK",
      "RELIANCE",
      "WIPRO",
    ]);
    const tcs = result.rows.find((row) => row.symbol === "TCS");
    expect(tcs?.sourceKeys).toEqual(["exmpl0000001", "twt", "vbt"]);
    expect(tcs?.count).toBe(3);
    const wipro = result.rows.find((row) => row.symbol === "WIPRO");
    expect(wipro?.sourceKeys).toEqual(["exmpl0000001"]);
    expect(result.summary).toMatch(/at least two/);
  });

  it("does not invent empty membership when a source could not be read", () => {
    const result = membershipOf([
      set("vbt", ["TCS"]),
      set("twt", null),
      set("swing", ["TCS", "INFY"]),
    ]);
    expect(result.available).toBe(true);
    expect(result.columns.find((column) => column.key === "twt")?.available).toBe(false);
    expect(result.rows).toHaveLength(2);
    expect(result.summary).toMatch(/could not be read/);
    expect(result.summary).toMatch(/omitted/);
  });

  it("says overlap cannot be computed when nothing could be read", () => {
    const result = membershipOf([set("vbt", null), set("twt", null)]);
    expect(result.available).toBe(false);
    expect(result.rows).toEqual([]);
    expect(result.summary).toMatch(/not the same as "no overlap"/);
  });

  it("lets the page keep screen-only names when asked, and hide them from the shared view", () => {
    const result = membershipOf([
      set("vbt", ["TCS"]),
      { key: "exmpl0000001", label: "Investing 001", symbols: ["TCS", "WIPRO"] },
    ]);
    expect(filterMembershipRows(result.rows, "shared", "exmpl0000001").map((row) => row.symbol)).toEqual(
      ["TCS"],
    );
    expect(filterMembershipRows(result.rows, "screen", "exmpl0000001").map((row) => row.symbol)).toEqual(
      ["TCS", "WIPRO"],
    );
    expect(filterMembershipRows(result.rows, "all", "exmpl0000001").map((row) => row.symbol)).toEqual([
      "TCS",
      "WIPRO",
    ]);
    expect(filterMembershipRows(result.rows, "three", "exmpl0000001")).toEqual([]);
  });
});
